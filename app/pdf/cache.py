"""On-disk document and translation caches (all under ./cache)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..logging_setup import get_logger
from ..models import DocumentStructure
from ..paths import CACHE_DIR

log = get_logger("cache")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


class DocumentCache:
    def __init__(self, source_path: Path, source_hash: str, config: AppConfig):
        self.source_path = Path(source_path).resolve()
        self.source_hash = source_hash
        self.config = config
        self.root = CACHE_DIR / source_hash[:20]
        self.structure_path = self.root / "structure.json"
        self.meta_path = self.root / "meta.json"
        self._signature: str | None = None

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def invalidate_signature(self) -> None:
        self._signature = None

    # --------------------------------------------------------------- structure
    def load_structure(self) -> DocumentStructure | None:
        if not self.structure_path.is_file():
            return None
        try:
            raw = json.loads(self.structure_path.read_text(encoding="utf-8"))
            if str(raw.get("source_hash", "")) != self.source_hash:
                return None
            doc = DocumentStructure.from_dict(raw)
            from .extractor import EXTRACTOR_VERSION

            if doc.extractor_version != EXTRACTOR_VERSION:
                log.info(
                    "structure cache extractor version changed (%s -> %s); reparsing",
                    doc.extractor_version,
                    EXTRACTOR_VERSION,
                )
                return None
            if doc.page_count <= 0 or not doc.pages:
                return None
            return doc
        except Exception:
            log.warning("invalid structure cache %s", self.structure_path, exc_info=True)
            return None

    def save_structure(self, document: DocumentStructure) -> None:
        self.ensure()
        _atomic_json(self.structure_path, document.to_dict())
        _atomic_json(
            self.meta_path,
            {
                "source_path": str(self.source_path),
                "source_hash": self.source_hash,
                "file_name": self.source_path.name,
                "saved_at": _now(),
                "title": document.title,
                "pages": document.page_count,
                "ocr": document.ocr_summary,
            },
        )

    # ------------------------------------------------------------- translation
    def translation_signature(self, document: DocumentStructure) -> str:
        if self._signature:
            return self._signature
        payload = {
            "content": document.content_hash(),
            "model": self.config.llm.model,
            "base_url": self.config.llm.base_url.rstrip("/"),
            "target": self.config.translation.target_language,
            "prompt": self.config.translation.prompt_version,
            "skip_references": self.config.translation.skip_references,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self._signature = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return self._signature

    def translation_path(self, signature: str) -> Path:
        return self.root / f"translation-{signature[:20]}.json"

    def load_translation(self, document: DocumentStructure) -> dict[str, str] | None:
        signature = self.translation_signature(document)
        path = self.translation_path(signature)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("signature") != signature:
                return None
            translations = raw.get("translations")
            if not isinstance(translations, dict):
                return None
            return {str(k): str(v) for k, v in translations.items() if str(v).strip()}
        except Exception:
            log.warning("invalid translation cache %s", path, exc_info=True)
            return None

    def save_translation(
        self,
        document: DocumentStructure,
        translations: dict[str, str],
        complete: bool = False,
    ) -> None:
        signature = self.translation_signature(document)
        payload = {
            "schema_version": 1,
            "signature": signature,
            "source_hash": self.source_hash,
            "model": self.config.llm.model,
            "base_url": self.config.llm.base_url.rstrip("/"),
            "target_language": self.config.translation.target_language,
            "updated_at": _now(),
            "complete": complete,
            "translations": translations,
        }
        _atomic_json(self.translation_path(signature), payload)

    def translation_meta(self, document: DocumentStructure) -> dict[str, Any] | None:
        path = self.translation_path(self.translation_signature(document))
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {
                "complete": bool(raw.get("complete")),
                "updated_at": raw.get("updated_at"),
                "model": raw.get("model"),
                "count": len(raw.get("translations") or {}),
            }
        except Exception:
            return None

    def clear_translation(self, document: DocumentStructure) -> None:
        path = self.translation_path(self.translation_signature(document))
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove translation cache %s", path, exc_info=True)

    def clear_all(self) -> None:
        import shutil

        try:
            shutil.rmtree(self.root, ignore_errors=True)
        except OSError:
            log.warning("could not remove cache dir %s", self.root, exc_info=True)
