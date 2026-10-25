"""Structured document model shared by the GUI, cache and OCR pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2
TRANSLATABLE_KINDS = {"title", "heading", "paragraph", "caption", "table"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Word:
    text: str
    bbox: list[float]
    line: str
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"t": self.text, "b": [round(v, 2) for v in self.bbox], "l": self.line, "o": self.order}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Word":
        return cls(
            text=str(raw.get("t", "")),
            bbox=[float(v) for v in raw.get("b", [0, 0, 0, 0])],
            line=str(raw.get("l", "")),
            order=int(raw.get("o", 0)),
        )


@dataclass
class Block:
    id: str
    page: int
    order: int
    kind: str
    text: str
    bbox: list[float] = field(default_factory=lambda: [0, 0, 0, 0])
    level: int = 0
    source: str = "native"  # native | ocr
    meta: dict[str, Any] = field(default_factory=dict)
    translation: str = ""
    hidden: bool = False

    @property
    def translatable(self) -> bool:
        return (
            not self.hidden
            and self.kind in TRANSLATABLE_KINDS
            and bool(self.text.strip())
            and not self.meta.get("region_image")
        )

    @property
    def display_label(self) -> str:
        return {
            "title": "标题",
            "heading": "章节",
            "paragraph": "正文",
            "table": "表格",
            "formula": "公式",
            "caption": "图注",
            "figure": "图像",
            "reference": "参考文献",
            "other": "其他",
        }.get(self.kind, "正文")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "page": self.page,
            "order": self.order,
            "type": self.kind,
            "text": self.text,
            "bbox": [round(v, 2) for v in self.bbox],
            "level": self.level,
            "source": self.source,
        }
        if self.meta:
            payload["meta"] = self.meta
        if self.hidden:
            payload["hidden"] = True
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Block":
        return cls(
            id=str(raw.get("id", "")),
            page=int(raw.get("page", 1)),
            order=int(raw.get("order", 0)),
            kind=str(raw.get("type", raw.get("kind", "paragraph"))),
            text=str(raw.get("text", "")),
            bbox=[float(v) for v in raw.get("bbox", [0, 0, 0, 0])],
            level=int(raw.get("level", 0)),
            source=str(raw.get("source", "native")),
            meta=dict(raw.get("meta") or {}),
            translation=str(raw.get("translation", "")),
            hidden=bool(raw.get("hidden", False)),
        )


@dataclass
class PageData:
    number: int
    width: float
    height: float
    blocks: list[Block] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    needs_ocr: bool = False
    ocr_done: bool = False
    ocr_engine: str = ""
    text_chars: int = 0
    image_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "width": round(self.width, 2),
            "height": round(self.height, 2),
            "needs_ocr": self.needs_ocr,
            "ocr_done": self.ocr_done,
            "ocr_engine": self.ocr_engine,
            "text_chars": self.text_chars,
            "image_ratio": round(self.image_ratio, 4),
            "blocks": [b.to_dict() for b in self.blocks],
            "words": [w.to_dict() for w in self.words],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PageData":
        return cls(
            number=int(raw.get("number", 1)),
            width=float(raw.get("width", 595)),
            height=float(raw.get("height", 842)),
            blocks=[Block.from_dict(b) for b in raw.get("blocks", [])],
            words=[Word.from_dict(w) for w in raw.get("words", [])],
            needs_ocr=bool(raw.get("needs_ocr", False)),
            ocr_done=bool(raw.get("ocr_done", False)),
            ocr_engine=str(raw.get("ocr_engine", "")),
            text_chars=int(raw.get("text_chars", 0)),
            image_ratio=float(raw.get("image_ratio", 0.0)),
        )


@dataclass
class DocumentStructure:
    source_path: str
    source_hash: str
    file_name: str
    title: str
    page_count: int
    pages: list[PageData]
    created_at: str = field(default_factory=_now_iso)
    schema_version: int = SCHEMA_VERSION
    authors: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    ocr_summary: dict[str, Any] = field(default_factory=dict)
    review_summary: dict[str, Any] = field(default_factory=dict)
    extractor_version: int = 1
    _block_index: dict[str, Block] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.reindex()
        if not self.title:
            self.title = Path(self.file_name or "Paper").stem

    def reindex(self) -> None:
        global_order = 0
        used: set[str] = set()
        for page in self.pages:
            for local_order, block in enumerate(page.blocks, start=1):
                global_order += 1
                block.order = local_order
                if not block.id or block.id in used:
                    block.id = f"p{page.number}_b{local_order}"
                # Guarantee uniqueness even for malformed cached data.
                candidate = block.id
                suffix = 2
                while candidate in used:
                    candidate = f"{block.id}_{suffix}"
                    suffix += 1
                block.id = candidate
                used.add(candidate)
        self._block_index = {b.id: b for p in self.pages for b in p.blocks}

    @property
    def blocks(self) -> list[Block]:
        result: list[Block] = []
        for page in self.pages:
            result.extend(page.blocks)
        return result

    def block(self, block_id: str) -> Block | None:
        return self._block_index.get(block_id)

    def translatable_blocks(self) -> list[Block]:
        return [b for b in self.blocks if b.translatable]

    def plain_text(self, include_translation: bool = False) -> str:
        parts: list[str] = []
        for block in self.blocks:
            if block.hidden or not block.text.strip():
                continue
            parts.append(f"[p.{block.page}] [{block.display_label}] {block.text.strip()}")
            if include_translation and block.translation.strip():
                parts.append(f"[译文] {block.translation.strip()}")
        return "\n\n".join(parts)

    def content_hash(self) -> str:
        digest = hashlib.sha256()
        for block in self.blocks:
            digest.update(block.id.encode("utf-8", "ignore"))
            digest.update(b"\0")
            digest.update(block.kind.encode("utf-8", "ignore"))
            digest.update(b"\0")
            digest.update(block.text.encode("utf-8", "ignore"))
            digest.update(b"\0")
        return digest.hexdigest()

    def stats(self) -> dict[str, int]:
        blocks = self.blocks
        return {
            "pages": len(self.pages),
            "blocks": len(blocks),
            "translatable": sum(1 for b in blocks if b.translatable),
            "tables": sum(1 for b in blocks if b.kind == "table"),
            "formulas": sum(1 for b in blocks if b.kind == "formula"),
            "captions": sum(1 for b in blocks if b.kind == "caption"),
            "ocr_pages": sum(1 for p in self.pages if p.ocr_done),
            "hidden": sum(1 for b in blocks if b.hidden),
        }

    def apply_translations(self, translations: dict[str, str]) -> None:
        for block_id, text in translations.items():
            block = self._block_index.get(block_id)
            if block is not None:
                block.translation = text

    def merged_translations(self) -> dict[str, str]:
        return {b.id: b.translation for b in self.blocks if b.translation}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "extractor_version": self.extractor_version,
            "source_path": self.source_path,
            "source_hash": self.source_hash,
            "file_name": self.file_name,
            "title": self.title,
            "authors": self.authors,
            "page_count": self.page_count,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "ocr_summary": self.ocr_summary,
            "review_summary": self.review_summary,
            "pages": [p.to_dict() for p in self.pages],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DocumentStructure":
        doc = cls(
            source_path=str(raw.get("source_path", "")),
            source_hash=str(raw.get("source_hash", "")),
            file_name=str(raw.get("file_name", "")),
            title=str(raw.get("title", "Untitled")),
            authors=str(raw.get("authors", "")),
            page_count=int(raw.get("page_count", 0)),
            pages=[PageData.from_dict(p) for p in raw.get("pages", [])],
            created_at=str(raw.get("created_at", _now_iso())),
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
            metadata=dict(raw.get("metadata") or {}),
            ocr_summary=dict(raw.get("ocr_summary") or {}),
            review_summary=dict(raw.get("review_summary") or {}),
            extractor_version=int(raw.get("extractor_version", 1)),
        )
        return doc

    def save_json(self, path: Any) -> None:
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load_json(cls, path: Any) -> "DocumentStructure":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_mostly_math(text: str) -> bool:
    if not text.strip():
        return False
    math_chars = sum(1 for ch in text if ch in "=+-*/^_{}[]()<>∑∫√∞≈≠≤≥±×÷·αβγδεζηθλμνξπρστφχψωΓΔΘΛΞΠΣΦΨΩ")
    letters = sum(1 for ch in text if ch.isalpha())
    return len(text) < 220 and math_chars >= 4 and math_chars / max(letters + math_chars, 1) > 0.18


def dedupe_keep_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
