"""Custom, lazy-rendering PDF viewer with word-level selection."""

from __future__ import annotations

import queue
import threading
from pathlib import Path

import pymupdf
from PySide6.QtCore import QPoint, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..logging_setup import get_logger
from ..models import DocumentStructure, PageData, Word
from . import theme as theme_module
from .widgets import SelectionPopup, ZoomScrollArea

log = get_logger("pdf.view")

PAGE_MARGIN = 22
PAGE_GAP = 18


class PdfRenderThread(QThread):
    page_ready = Signal(int, QImage, int)
    page_failed = Signal(int, str)

    def __init__(self, pdf_path: Path, parent=None):
        super().__init__(parent)
        self.pdf_path = str(pdf_path)
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._generation = 0
        self._lock = threading.Lock()

    def set_generation(self, generation: int) -> None:
        with self._lock:
            self._generation = generation

    def request(self, page_number: int, scale: float, dpr: float, generation: int) -> None:
        self._queue.put((page_number, scale, dpr, generation))

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        if self.isRunning():
            self.wait(3000)

    def run(self) -> None:
        try:
            doc = pymupdf.open(self.pdf_path)
        except Exception as exc:
            log.exception("render thread cannot open PDF")
            self.page_failed.emit(0, str(exc))
            return
        try:
            while not self._stop.is_set():
                try:
                    item = self._queue.get(timeout=0.15)
                except queue.Empty:
                    continue
                if item is None:
                    break
                page_number, scale, dpr, generation = item
                with self._lock:
                    if generation < self._generation:
                        continue
                try:
                    page = doc.load_page(page_number - 1)
                    render_scale = max(0.1, scale * dpr)
                    # Guard against pathological zoom levels on 4K pages.
                    pixels = page.rect.width * page.rect.height * render_scale * render_scale
                    if pixels > 24_000_000:
                        render_scale *= (24_000_000 / pixels) ** 0.5
                    pix = page.get_pixmap(
                        matrix=pymupdf.Matrix(render_scale, render_scale),
                        alpha=False,
                        colorspace=pymupdf.csRGB,
                    )
                    image = QImage(
                        pix.samples,
                        pix.width,
                        pix.height,
                        pix.stride,
                        QImage.Format.Format_RGB888,
                    ).copy()
                    image.setDevicePixelRatio(dpr)
                    self.page_ready.emit(page_number, image, generation)
                except Exception as exc:
                    log.warning("page render failed: page=%s %s", page_number, exc)
                    self.page_failed.emit(page_number, str(exc))
        finally:
            doc.close()


class PdfPageWidget(QWidget):
    selection_made = Signal(str, int, QPoint)
    selection_cleared = Signal()
    clicked = Signal()

    def __init__(self, page: PageData, parent=None):
        super().__init__(parent)
        self.page = page
        self.page_number = page.number
        self.page_width = page.width
        self.page_height = page.height
        self.scale = 1.0
        self.words: list[Word] = list(page.words)
        self._pixmap: QPixmap | None = None
        self._generation = -1
        self._anchor: int | None = None
        self._current: int | None = None
        self._render_error = ""
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._apply_size()

    def _apply_size(self) -> None:
        width = max(80, int(round(self.page_width * self.scale)))
        height = max(100, int(round(self.page_height * self.scale)))
        self.setFixedSize(QSize(width, height))
        self.update()

    def set_scale(self, scale: float) -> None:
        if abs(scale - self.scale) < 0.001:
            return
        self.scale = scale
        self._apply_size()

    def clear_image(self) -> None:
        self._pixmap = None
        self._generation = -1
        self.update()

    def set_image(self, image: QImage, generation: int) -> None:
        if generation < self._generation:
            return
        self._pixmap = QPixmap.fromImage(image)
        self._generation = generation
        self._render_error = ""
        self.update()

    def set_render_error(self, message: str) -> None:
        self._render_error = message
        self.update()

    # ------------------------------------------------------------------ paint
    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = self.rect()
        painter.fillRect(rect, QColor("#FFFFFF"))
        if self._pixmap is not None:
            painter.drawPixmap(rect, self._pixmap)
        else:
            painter.fillRect(rect, QColor("#FFFFFF"))
            painter.setPen(QColor("#C9C2B5"))
            font = QFont(self.font())
            font.setPointSize(9)
            painter.setFont(font)
            if self._render_error:
                painter.drawText(rect.adjusted(20, 20, -20, -20), Qt.AlignmentFlag.AlignCenter, "页面渲染失败")
            else:
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "正在渲染页面…")
        painter.setPen(QPen(QColor("#D7D0C3"), 1))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        self._paint_selection(painter)
        painter.end()

    def _selection_range(self) -> tuple[int, int] | None:
        if self._anchor is None or self._current is None:
            return None
        if not self.words:
            return None
        low = max(0, min(self._anchor, self._current))
        high = min(len(self.words) - 1, max(self._anchor, self._current))
        if high < low:
            return None
        return low, high

    def _paint_selection(self, painter: QPainter) -> None:
        selected = self._selection_range()
        if not selected:
            return
        low, high = selected
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(15, 118, 110, 62))
        for word in self.words[low : high + 1]:
            x0, y0, x1, y1 = [v * self.scale for v in word.bbox]
            painter.drawRoundedRect(
                QRect(int(x0) - 1, int(y0) - 1, max(2, int(x1 - x0) + 2), max(2, int(y1 - y0) + 2)),
                2,
                2,
            )
        painter.setBrush(Qt.BrushStyle.NoBrush)

    # ------------------------------------------------------------- hit testing
    def word_at(self, point: QPoint) -> int | None:
        if not self.words:
            return None
        best: tuple[float, int] | None = None
        for index, word in enumerate(self.words):
            x0, y0, x1, y1 = [v * self.scale for v in word.bbox]
            expanded = QRect(int(x0) - 2, int(y0) - 2, max(2, int(x1 - x0) + 4), max(2, int(y1 - y0) + 4))
            if expanded.contains(point):
                cx = (x0 + x1) / 2
                cy = (y0 + y1) / 2
                distance = (point.x() - cx) ** 2 + (point.y() - cy) ** 2
                if best is None or distance < best[0]:
                    best = (distance, index)
        if best:
            return best[1]
        # Snap to the nearest word within a comfortable radius.
        nearest: tuple[float, int] | None = None
        for index, word in enumerate(self.words):
            x0, y0, x1, y1 = [v * self.scale for v in word.bbox]
            cx = (x0 + x1) / 2
            cy = (y0 + y1) / 2
            distance = ((point.x() - cx) ** 2 + (point.y() - cy) ** 2) ** 0.5
            if distance <= 16 and (nearest is None or distance < nearest[0]):
                nearest = (distance, index)
        return nearest[1] if nearest else None

    def selection_text(self) -> str:
        selected = self._selection_range()
        if not selected:
            return ""
        low, high = selected
        chosen = self.words[low : high + 1]
        pieces: list[str] = []
        previous_line: str | None = None
        for word in chosen:
            if previous_line is not None:
                if word.line != previous_line:
                    pieces.append("\n")
                else:
                    last = pieces[-1][-1:] if pieces else ""
                    first = word.text[:1]
                    if not (_is_cjk(last) and _is_cjk(first)):
                        pieces.append(" ")
            pieces.append(word.text)
            previous_line = word.line
        return "".join(pieces).strip()

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self.clicked.emit()
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.word_at(event.position().toPoint())
            self._anchor = index
            self._current = index
            self.update()
            if index is None:
                self.selection_cleared.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.buttons() & Qt.MouseButton.LeftButton and self._anchor is not None:
            index = self.word_at(event.position().toPoint())
            if index is not None:
                self._current = index
                self.update()
            self.setCursor(Qt.CursorShape.IBeamCursor)
            event.accept()
            return
        if self.word_at(event.position().toPoint()) is not None:
            self.setCursor(Qt.CursorShape.IBeamCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            if self._anchor is not None:
                index = self.word_at(event.position().toPoint())
                if index is not None:
                    self._current = index
            text = self.selection_text()
            if text:
                self.selection_made.emit(text, self.page_number, event.globalPosition().toPoint())
            else:
                self.selection_cleared.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        index = self.word_at(event.position().toPoint())
        if index is not None:
            self._anchor = index
            self._current = index
            text = self.selection_text()
            self.update()
            if text:
                self.selection_made.emit(text, self.page_number, event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        text = self.selection_text()
        menu = QMenu(self)
        copy_action = menu.addAction("复制选中原文")
        copy_action.setEnabled(bool(text))
        quote_action = menu.addAction("引用并提问")
        quote_action.setEnabled(bool(text))
        chosen = menu.exec(event.globalPos())
        if chosen == copy_action and text:
            QApplication.clipboard().setText(text)
        elif chosen == quote_action and text:
            self.selection_made.emit(text, self.page_number, event.globalPos())

    def clear_selection(self) -> None:
        self._anchor = None
        self._current = None
        self.update()


class PdfViewer(QWidget):
    quote_requested = Signal(str, int)
    ask_requested = Signal(str, int)
    visible_page_changed = Signal(int, int)
    zoom_changed = Signal(int)
    status_message = Signal(str)
    selection_state = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.structure: DocumentStructure | None = None
        self.pdf_path: Path | None = None
        self.pages: list[PdfPageWidget] = []
        self._render_thread: PdfRenderThread | None = None
        self._generation = 0
        self._pending: set[int] = set()
        self._zoom = 1.0
        self._fit_scale = 1.0
        self._last_viewport_width = 0
        self._selection_text = ""
        self._selection_page = 0
        self._visible_timer = QTimer(self)
        self._visible_timer.setSingleShot(True)
        self._visible_timer.setInterval(90)
        self._visible_timer.timeout.connect(self._refresh_visible)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(120)
        self._resize_timer.timeout.connect(self._on_resize_settled)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.scroll = ZoomScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.container = QWidget()
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(PAGE_MARGIN, 20, PAGE_MARGIN, 30)
        self.container_layout.setSpacing(PAGE_GAP)
        self.container_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll)
        self.empty = QLabel("打开一篇 PDF 开始阅读", self.container)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.container_layout.addWidget(self.empty, 0, Qt.AlignmentFlag.AlignHCenter)

        self.selection_popup = SelectionPopup(self)
        self.selection_popup.quote_requested.connect(self._emit_quote)
        self.selection_popup.ask_requested.connect(self._emit_ask)
        self.selection_popup.closed.connect(self._hide_popup)

        self.scroll.ctrl_wheel.connect(self.zoom_by)
        self.scroll.verticalScrollBar().valueChanged.connect(self._schedule_visible)
        self.scroll.horizontalScrollBar().valueChanged.connect(self._schedule_visible)
        self.apply_appearance()

    def apply_appearance(self) -> None:
        c = theme_module.COLORS
        background = c.get("pdf_bg", "#E7E3DA")
        self.scroll.setStyleSheet(f"QScrollArea {{ background:{background}; }}")
        self.container.setStyleSheet(f"background:{background};")
        self.empty.setStyleSheet(
            f"color:{c.get('muted', '#8A8E88')}; font-size:13pt; padding:80px;"
        )
        self.update()

    # --------------------------------------------------------------- lifecycle
    def set_document(self, pdf_path: Path, structure: DocumentStructure) -> None:
        self._stop_render()
        self._clear_pages()
        self.pdf_path = Path(pdf_path)
        self.structure = structure
        self._zoom = 1.0
        self._generation += 1
        self._pending.clear()
        self._render_thread = PdfRenderThread(self.pdf_path, self)
        self._render_thread.page_ready.connect(self._on_page_ready)
        self._render_thread.page_failed.connect(self._on_page_failed)
        self._render_thread.start()
        self.empty.hide()
        for page in structure.pages:
            widget = PdfPageWidget(page, self.container)
            widget.selection_made.connect(self._on_selection_made)
            widget.selection_cleared.connect(self._on_selection_cleared)
            widget.clicked.connect(self._hide_popup)
            self.container_layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
            self.pages.append(widget)
        self._fit_scale = 1.0
        QTimer.singleShot(30, self._initial_fit)
        self._schedule_visible()
        self.zoom_changed.emit(100)

    def clear_document(self) -> None:
        self._stop_render()
        self._clear_pages()
        self.structure = None
        self.pdf_path = None
        self.empty.show()
        self.zoom_changed.emit(100)

    def _clear_pages(self) -> None:
        self._hide_popup()
        for widget in self.pages:
            widget.setParent(None)
            widget.deleteLater()
        self.pages = []
        self._pending.clear()

    def _stop_render(self) -> None:
        if self._render_thread is not None:
            self._render_thread.stop()
            self._render_thread.deleteLater()
            self._render_thread = None

    def shutdown(self) -> None:
        self._stop_render()

    # ------------------------------------------------------------------ layout
    def _initial_fit(self) -> None:
        if not self.pages:
            return
        self._recompute_fit()
        self._apply_scale()
        self._schedule_visible()
        QTimer.singleShot(60, self._refresh_visible)

    def _recompute_fit(self) -> None:
        if not self.pages:
            return
        viewport = self.scroll.viewport()
        width = max(260, viewport.width() - 2 * PAGE_MARGIN - 6)
        max_page_width = max(p.page_width for p in self.pages) or 595
        self._fit_scale = max(0.32, min(width / max_page_width, 1.45))

    def _apply_scale(self) -> None:
        if not self.pages:
            return
        scale = self._fit_scale * self._zoom
        changed = any(abs(page.scale - scale) > 0.002 for page in self.pages)
        if not changed:
            return
        self._generation += 1
        self._pending.clear()
        if self._render_thread:
            self._render_thread.set_generation(self._generation)
        for page in self.pages:
            page.set_scale(scale)
            page.clear_image()
        self.zoom_changed.emit(int(round(self._zoom * 100)))
        self._schedule_visible()

    def _on_resize_settled(self) -> None:
        if not self.pages:
            return
        width = self.scroll.viewport().width()
        if abs(width - self._last_viewport_width) < 2:
            return
        self._last_viewport_width = width
        old_fit = self._fit_scale
        self._recompute_fit()
        if abs(old_fit - self._fit_scale) > 0.002:
            self._apply_scale()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._resize_timer.start()
        self._schedule_visible()

    def zoom_by(self, step: int) -> None:
        if not self.pages:
            return
        factor = 1.15 if step > 0 else 1 / 1.15
        new_zoom = max(0.4, min(4.0, self._zoom * factor))
        if abs(new_zoom - self._zoom) < 0.001:
            return
        self._zoom = new_zoom
        self._apply_scale()
        self._schedule_visible()

    def reset_zoom(self) -> None:
        if not self.pages:
            return
        self._zoom = 1.0
        self._apply_scale()
        self._schedule_visible()

    def go_to_page(self, page_number: int) -> None:
        if not self.pages:
            return
        page_number = max(1, min(page_number, len(self.pages)))
        widget = self.pages[page_number - 1]
        self.scroll.ensureWidgetVisible(widget, 0, 10)
        self._schedule_visible()

    # --------------------------------------------------------------- rendering
    def _schedule_visible(self) -> None:
        self._hide_popup()
        self._visible_timer.start()

    def _refresh_visible(self) -> None:
        if not self.pages or not self._render_thread:
            return
        viewport_rect = self.scroll.viewport().rect().adjusted(0, -500, 0, 500)
        visible_indices: list[int] = []
        for index, widget in enumerate(self.pages):
            top_left = widget.mapTo(self.scroll.viewport(), QPoint(0, 0))
            rect = QRect(top_left, widget.size())
            if rect.intersects(viewport_rect):
                visible_indices.append(index)
                if index not in self._pending and widget._pixmap is None:
                    self._pending.add(index)
                    dpr = max(1.0, min(1.75, self.devicePixelRatioF()))
                    self._render_thread.request(
                        widget.page_number,
                        widget.scale,
                        dpr,
                        self._generation,
                    )
        # Evict distant page bitmaps to keep memory bounded.
        if visible_indices:
            low = max(0, min(visible_indices) - 2)
            high = min(len(self.pages) - 1, max(visible_indices) + 2)
            for index, widget in enumerate(self.pages):
                if index < low or index > high:
                    widget.clear_image()
            top_index = self._current_top_page(visible_indices)
            if top_index is not None:
                self.visible_page_changed.emit(top_index + 1, len(self.pages))

    def _current_top_page(self, visible_indices: list[int]) -> int | None:
        if not visible_indices:
            return None
        viewport_top = self.scroll.viewport().mapToGlobal(QPoint(0, 0)).y()
        best = visible_indices[0]
        for index in visible_indices:
            widget = self.pages[index]
            top = widget.mapToGlobal(QPoint(0, 0)).y()
            if top <= viewport_top + 30:
                best = index
        return best

    def _on_page_ready(self, page_number: int, image: QImage, generation: int) -> None:
        self._pending.discard(page_number - 1)
        if generation != self._generation:
            return
        if 1 <= page_number <= len(self.pages):
            self.pages[page_number - 1].set_image(image, generation)

    def _on_page_failed(self, page_number: int, message: str) -> None:
        if page_number > 0:
            self._pending.discard(page_number - 1)
            if page_number <= len(self.pages):
                self.pages[page_number - 1].set_render_error(message)
        self.status_message.emit(f"页面 {page_number} 渲染失败：{message}")

    # --------------------------------------------------------------- selection
    def _on_selection_made(self, text: str, page: int, global_pos: QPoint) -> None:
        self._selection_text = text
        self._selection_page = page
        self.selection_state.emit(True)
        self.selection_popup.show_for(text, global_pos)

    def _on_selection_cleared(self) -> None:
        self._selection_text = ""
        self._selection_page = 0
        self.selection_state.emit(False)
        self._hide_popup()

    def _hide_popup(self) -> None:
        if hasattr(self, "selection_popup"):
            self.selection_popup.hide()

    def _emit_quote(self) -> None:
        if self._selection_text:
            self.quote_requested.emit(self._selection_text, self._selection_page)

    def _emit_ask(self) -> None:
        if self._selection_text:
            self.ask_requested.emit(self._selection_text, self._selection_page)

    def clear_selection(self) -> None:
        for page in self.pages:
            page.clear_selection()
        self._on_selection_cleared()


def _is_cjk(value: str) -> bool:
    return bool(value) and "\u3400" <= value[0] <= "\u9fff"
