"""Background workers.  Nothing that touches network or PDF parsing runs on
the GUI thread."""

from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from .config import AppConfig
from .llm import (
    TranslationService,
    build_chat_messages,
    build_chat_context,
    estimate_tokens,
    friendly_api_error,
    stream_chat,
    test_connection,
)
from .logging_setup import get_logger
from .models import Block, DocumentStructure
from .pdf.cache import DocumentCache
from .pdf.extractor import (
    ExtractionCancelled,
    extract_document,
    hash_file,
    ocr_target_pages,
)
from .review import review_translation

log = get_logger("workers")


class PrepareWorker(QThread):
    progress = Signal(str, int, int, str)
    ready = Signal(object, object, list)
    failed = Signal(str)

    def __init__(
        self,
        pdf_path: Path,
        config: AppConfig,
        force_reparse: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.pdf_path = Path(pdf_path)
        self.config = config.copy()
        self.force_reparse = force_reparse
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        try:
            self.progress.emit("hash", 0, 1, "正在计算 PDF 指纹…")
            digest = hash_file(self.pdf_path)
            cache = DocumentCache(self.pdf_path, digest, self.config)
            document = None if self.force_reparse else cache.load_structure()
            if document is not None:
                self.progress.emit("cache", 1, 1, "已载入本地结构化缓存")
                log.info("loaded structure cache for %s", self.pdf_path.name)
            else:
                if self._cancelled.is_set():
                    raise ExtractionCancelled()
                document = extract_document(
                    self.pdf_path,
                    self.config,
                    progress=lambda phase, cur, total, msg: self.progress.emit(phase, cur, total, msg),
                    cancel=self._cancelled.is_set,
                    known_hash=digest,
                    asset_dir=cache.root / "assets",
                )
                cache.save_structure(document)
                self.progress.emit("cache", 1, 1, "结构化 JSON 缓存已写入 cache/")
            if self._cancelled.is_set():
                raise ExtractionCancelled()
            targets = ocr_target_pages(document, self.config)
            self.ready.emit(document, cache, targets)
        except ExtractionCancelled:
            self.failed.emit("已取消")
        except Exception as exc:
            log.exception("prepare failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class TranslationWorker(QThread):
    progress = Signal(int, int, str)
    partial = Signal(object)
    finished_ok = Signal(object, bool, str)  # translations, complete, note
    failed = Signal(str, object)

    def __init__(
        self,
        document: DocumentStructure,
        cache: DocumentCache,
        config: AppConfig,
        existing: dict[str, str],
        parent=None,
    ):
        super().__init__(parent)
        self.document = document
        self.cache = cache
        self.config = config.copy()
        self.existing = dict(existing)
        self._cancelled = threading.Event()
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        started = time.perf_counter()
        try:
            blocks = [
                b
                for b in self.document.translatable_blocks()
                if b.text.strip() and b.id not in self.existing
            ]
            if self.config.translation.skip_references:
                blocks = [b for b in blocks if b.kind != "reference"]
            if not blocks:
                self.finished_ok.emit(self.existing, True, "所有内容均已有译文")
                return

            service = TranslationService(self.config)
            chunks = _chunk_blocks(blocks, self.config.translation.chunk_chars)
            total = len(chunks)
            self.progress.emit(0, total, f"准备翻译 {len(blocks)} 个结构块（{total} 批）…")
            translations = dict(self.existing)
            errors: list[str] = []
            workers = max(1, min(self.config.translation.max_workers, total))
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="easypaper-translate"
            )
            futures = {
                executor.submit(service.translate_blocks, chunk, self.document.title): chunk
                for chunk in chunks
            }
            completed = 0
            try:
                for future in concurrent.futures.as_completed(futures):
                    if self._cancelled.is_set():
                        break
                    chunk = futures[future]
                    try:
                        mapping = future.result()
                    except Exception as exc:
                        message = friendly_api_error(exc)
                        errors.append(message)
                        log.warning("translation chunk failed: %s", exc)
                        completed += 1
                        self.progress.emit(completed, total, f"有一批翻译失败：{message}")
                        continue
                    with self._lock:
                        translations.update(mapping)
                        self.cache.save_translation(self.document, translations, complete=False)
                    completed += 1
                    self.partial.emit(dict(mapping))
                    elapsed = time.perf_counter() - started
                    self.progress.emit(
                        completed,
                        total,
                        f"已翻译 {completed}/{total} 批 · {len(translations)} 块 · {elapsed:.1f}s",
                    )
            finally:
                if self._cancelled.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                else:
                    executor.shutdown(wait=True, cancel_futures=True)

            if self._cancelled.is_set():
                self.failed.emit("翻译已取消，已完成的译文已缓存。", dict(translations))
                return
            complete = not errors
            note = "翻译完成" if complete else f"部分批次失败（{len(errors)} 批），已保留成功译文"
            self.cache.save_translation(self.document, translations, complete=complete)
            self.finished_ok.emit(translations, complete, note)
        except Exception as exc:
            log.exception("translation failed")
            self.failed.emit(friendly_api_error(exc), {})


def _chunk_blocks(blocks: list[Block], max_chars: int) -> list[list[Block]]:
    chunks: list[list[Block]] = []
    current: list[Block] = []
    size = 0
    for block in blocks:
        length = len(block.text) + 80
        if current and size + length > max_chars:
            chunks.append(current)
            current = []
            size = 0
        current.append(block)
        size += length
    if current:
        chunks.append(current)
    return chunks


class ChatWorker(QThread):
    delta = Signal(str)
    finished_ok = Signal(str, object)
    failed = Signal(str, object)

    def __init__(
        self,
        config: AppConfig,
        document: DocumentStructure,
        question: str,
        history: list[dict[str, str]],
        selected_text: str = "",
        selected_page: int = 0,
        parent=None,
    ):
        super().__init__(parent)
        self.config = config.copy()
        self.document = document
        self.question = question
        self.history = list(history)
        self.selected_text = selected_text
        self.selected_page = selected_page
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        started = time.perf_counter()
        try:
            context, meta = build_chat_context(
                self.document,
                self.config,
                self.question,
                self.selected_text,
                self.selected_page,
            )
            if not context.strip():
                self.failed.emit("当前论文没有可供问答的文本内容。", {})
                return
            messages = build_chat_messages(self.config, context, self.question, self.history)
            answer = stream_chat(
                self.config,
                messages,
                on_delta=self.delta.emit,
                cancelled=self._cancelled.is_set,
            )
            if self._cancelled.is_set():
                self.finished_ok.emit(answer, {**meta, "cancelled": True})
                return
            if not answer.strip():
                self.failed.emit("模型没有返回内容。", meta)
                return
            meta["elapsed"] = round(time.perf_counter() - started, 2)
            meta["answer_chars"] = len(answer)
            meta["tokens_est"] = estimate_tokens(context) + estimate_tokens(answer)
            self.finished_ok.emit(answer, meta)
        except Exception as exc:
            log.exception("chat failed")
            self.failed.emit(friendly_api_error(exc), {})


class ReviewWorker(QThread):
    finished_ok = Signal(list, dict)
    failed = Signal(str)

    def __init__(
        self,
        config: AppConfig,
        document: DocumentStructure,
        candidates: list[Block],
        parent=None,
    ):
        super().__init__(parent)
        self.config = config.copy()
        self.document = document
        self.candidates = candidates

    def run(self) -> None:
        try:
            removed = review_translation(self.config, self.document, self.candidates)
            self.finished_ok.emit(
                removed,
                {
                    "candidates": len(self.candidates),
                    "removed": len(removed),
                    "model": self.config.llm.model,
                },
            )
        except Exception as exc:
            log.exception("review failed")
            self.failed.emit(friendly_api_error(exc))


class ConnectionTestWorker(QThread):
    result_ready = Signal(object)

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config.copy()

    def run(self) -> None:
        try:
            self.result_ready.emit(test_connection(self.config))
        except Exception as exc:
            self.result_ready.emit(
                {
                    "ok": False,
                    "message": friendly_api_error(exc),
                    "latency_ms": 0,
                    "models": [],
                    "model_found": False,
                    "detail": str(exc),
                }
            )
