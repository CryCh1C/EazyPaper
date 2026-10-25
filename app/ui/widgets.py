from __future__ import annotations

from PySide6.QtCore import QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


class ZoomScrollArea(QScrollArea):
    """Scroll area that reports Ctrl+wheel instead of scrolling."""

    ctrl_wheel = Signal(int)

    def wheelEvent(self, event):  # type: ignore[override]
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.ctrl_wheel.emit(1 if delta > 0 else -1)
                event.accept()
                return
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            delta = event.angleDelta().y() or event.angleDelta().x()
            if delta:
                bar = self.horizontalScrollBar()
                bar.setValue(bar.value() - delta)
                event.accept()
                return
        super().wheelEvent(event)


class ZoomTextBrowser(QTextBrowser):
    """Read-only rich text view with persistent Ctrl+wheel zoom."""

    ctrl_wheel = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.zoom_steps = 0
        self.external_zoom = False
        self.setOpenExternalLinks(True)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().setDocumentMargin(16)

    def wheelEvent(self, event):  # type: ignore[override]
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                step = 1 if delta > 0 else -1
                if not self.external_zoom:
                    self.zoom_by(step)
                self.ctrl_wheel.emit(step)
                event.accept()
                return
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            delta = event.angleDelta().y() or event.angleDelta().x()
            if delta:
                bar = self.horizontalScrollBar()
                bar.setValue(bar.value() - delta)
                event.accept()
                return
        super().wheelEvent(event)

    def zoom_by(self, step: int) -> None:
        self.zoom_steps = max(-8, min(14, self.zoom_steps + step))
        if step > 0:
            self.zoomIn(1)
        elif step < 0:
            self.zoomOut(1)

    def reset_zoom(self) -> None:
        while self.zoom_steps > 0:
            self.zoomOut(1)
            self.zoom_steps -= 1
        while self.zoom_steps < 0:
            self.zoomIn(1)
            self.zoom_steps += 1

    def top_anchor(self) -> str:
        """Return the anchor name at the top-left of the visible content."""

        try:
            position = self.cursorForPosition(QPoint(8, 6)).position()
        except Exception:
            return ""
        best = ""
        best_pos = -1
        block = self.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    names = fragment.charFormat().anchorNames()
                    if names and block.position() <= position and block.position() >= best_pos:
                        best = names[0]
                        best_pos = block.position()
                iterator += 1
            block = block.next()
        return best

    def set_html(self, html: str, keep_position: bool = True) -> None:
        bar = self.verticalScrollBar()
        value = bar.value()
        anchor = self.top_anchor() if keep_position else ""
        self.setHtml(html)
        for _ in range(abs(self.zoom_steps)):
            if self.zoom_steps > 0:
                self.zoomIn(1)
            else:
                self.zoomOut(1)
        if keep_position:
            if anchor:
                self.scrollToAnchor(anchor)
                QTimer.singleShot(0, lambda name=anchor: self.scrollToAnchor(name))
            else:
                bar.setValue(min(value, bar.maximum()))
        else:
            bar.setValue(0)

    def append_stream_text(self, text: str) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()


class SelectionPopup(QFrame):
    quote_requested = Signal()
    ask_requested = Signal()
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SelectionPopup")
        self.setStyleSheet(
            """
            QFrame#SelectionPopup {
                background: #20262E;
                border: 1px solid #3B4450;
                border-radius: 9px;
            }
            QFrame#SelectionPopup QLabel {
                color: #E9E5DC;
                font-size: 9pt;
            }
            QFrame#SelectionPopup QPushButton {
                background: #2C3540;
                color: #F4F0E7;
                border: 1px solid #4A5563;
                border-radius: 6px;
                padding: 3px 9px;
                font-size: 8.5pt;
            }
            QFrame#SelectionPopup QPushButton:hover {
                background: #0F766E;
                border-color: #0F766E;
            }
            QFrame#SelectionPopup QPushButton#Ask {
                background: #0F766E;
                border-color: #0F766E;
            }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 8, 7)
        layout.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.snippet = QLabel("已选中原文")
        self.snippet.setMaximumWidth(420)
        self.snippet.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        top.addWidget(self.snippet, 1)
        close = QPushButton("×")
        close.setFixedSize(22, 22)
        close.setToolTip("关闭")
        close.clicked.connect(self._close)
        top.addWidget(close, 0)
        layout.addLayout(top)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        quote = QPushButton("引用提问")
        quote.clicked.connect(self._quote)
        ask = QPushButton("立即提问")
        ask.setObjectName("Ask")
        ask.clicked.connect(self._ask)
        actions.addStretch(1)
        actions.addWidget(quote)
        actions.addWidget(ask)
        layout.addLayout(actions)
        self.hide()

    def show_for(self, text: str, global_pos: QPoint) -> None:
        snippet = " ".join(text.split())
        if len(snippet) > 100:
            snippet = snippet[:100] + "…"
        self.snippet.setText(snippet)
        self.adjustSize()
        parent = self.parentWidget()
        if parent:
            local = parent.mapFromGlobal(global_pos)
            x = max(6, min(local.x() - 30, parent.width() - self.width() - 6))
            y = local.y() + 12
            if y + self.height() > parent.height() - 6:
                y = max(6, local.y() - self.height() - 12)
            self.move(x, y)
        self.show()
        self.raise_()

    def _quote(self) -> None:
        self.hide()
        self.quote_requested.emit()

    def _ask(self) -> None:
        self.hide()
        self.ask_requested.emit()

    def _close(self) -> None:
        self.hide()
        self.closed.emit()


def make_divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color: #ECE7DC;")
    return line


def set_font_size(widget: QWidget, size: float) -> None:
    font = QFont(widget.font())
    font.setPointSizeF(size)
    widget.setFont(font)
