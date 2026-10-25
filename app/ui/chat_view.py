from __future__ import annotations

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme as theme_module
from .richtext import render_chat_messages
from .widgets import ZoomTextBrowser


class ChatView(QWidget):
    ask_requested = Signal(str, str, int)
    zoom_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.messages: list[dict] = []
        self._streaming = False
        self._stream_buffer = ""
        self._meta = ""
        self._quote_text = ""
        self._quote_page = 0
        self._last_request_history: list[dict] = []
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(55)
        self._render_timer.timeout.connect(self._flush_stream)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.transcript = ZoomTextBrowser(self)
        self.transcript.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.transcript.ctrl_wheel.connect(self.zoom_by)
        layout.addWidget(self.transcript, 1)

        self.quote_frame = QFrame(self)
        quote_layout = QHBoxLayout(self.quote_frame)
        quote_layout.setContentsMargins(11, 6, 8, 6)
        quote_layout.setSpacing(5)
        self.quote_label = QLabel("引用：")
        self.quote_label.setWordWrap(False)
        quote_layout.addWidget(self.quote_label, 1)
        close_quote = QPushButton("×")
        close_quote.setToolTip("移除引用")
        close_quote.setFixedSize(22, 22)
        close_quote.clicked.connect(self.clear_quote)
        quote_layout.addWidget(close_quote, 0)
        self.quote_frame.hide()
        layout.addWidget(self.quote_frame, 0)

        input_frame = QFrame(self)
        self.input_frame = input_frame
        input_layout = QVBoxLayout(input_frame)
        input_layout.setContentsMargins(9, 8, 9, 9)
        input_layout.setSpacing(6)
        self.input = QPlainTextEdit(input_frame)
        self.input.setPlaceholderText("输入问题…")
        self.input.setFixedHeight(82)
        self.input.installEventFilter(self)
        self.input.textChanged.connect(self._update_send_enabled)
        input_layout.addWidget(self.input)
        bottom = QHBoxLayout()
        bottom.setSpacing(7)
        self.hint = QLabel("")
        self.hint.setObjectName("Muted")
        bottom.addWidget(self.hint, 1)
        self.clear_button = QPushButton("清空")
        self.clear_button.setFixedHeight(28)
        self.clear_button.clicked.connect(self.clear_conversation)
        bottom.addWidget(self.clear_button, 0)
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("Primary")
        self.send_button.setFixedHeight(28)
        self.send_button.clicked.connect(self.send)
        self.send_button.setEnabled(False)
        bottom.addWidget(self.send_button, 0)
        input_layout.addLayout(bottom)
        layout.addWidget(input_frame, 0)

        self._document_ready = False
        self._zoom_steps = 0
        self.apply_appearance()
        self._refresh()

    def apply_appearance(self) -> None:
        c = theme_module.COLORS
        self.transcript.setStyleSheet(
            f"QTextBrowser {{ background:{c.get('panel_alt', '#FAF8F4')}; "
            f"border:none; color:{c.get('ink', '#20262E')}; }}"
        )
        self.quote_frame.setStyleSheet(
            f"QFrame {{ background:{c.get('warm_soft', '#FBF7F1')}; "
            f"border-top:1px solid {c.get('border', '#E8DED0')}; }}"
            f"QLabel {{ color:{c.get('muted', '#6E6252')}; font-size:8.5pt; }}"
            f"QPushButton {{ border:none; background:transparent; color:{c.get('warm', '#8C7B5F')}; "
            "font-size:11pt; padding:0 6px; }"
        )
        self.input_frame.setStyleSheet(
            f"QFrame {{ background:{c.get('panel', '#FFFFFF')}; "
            f"border-top:1px solid {c.get('border', '#E6E1D6')}; }}"
        )

    # --------------------------------------------------------------- document
    def set_document_ready(self, ready: bool) -> None:
        self._document_ready = ready
        self.input.setEnabled(ready)
        self._update_send_enabled()
        if not ready:
            self.input.setPlaceholderText("打开 PDF 后提问")
        else:
            self.input.setPlaceholderText("输入问题…")

    def reset_document(self) -> None:
        self.messages = []
        self._streaming = False
        self._stream_buffer = ""
        self._meta = ""
        self.clear_quote()
        self._document_ready = False
        self.input.clear()
        self.set_document_ready(False)
        self._refresh()

    def restore_messages(self, messages: list[dict]) -> None:
        restored: list[dict] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", ""))
            content = str(item.get("content", ""))
            if role not in {"user", "assistant"} or not content.strip():
                continue
            row: dict = {"role": role, "content": content}
            if item.get("quote"):
                row["quote"] = str(item.get("quote"))
            if item.get("quote_page"):
                try:
                    row["quote_page"] = int(item.get("quote_page"))
                except (TypeError, ValueError):
                    pass
            if item.get("meta"):
                row["meta"] = str(item.get("meta"))
            restored.append(row)
        self.messages = restored
        self._streaming = False
        self._stream_buffer = ""
        self._meta = ""
        self.clear_quote()
        self._document_ready = True
        self.input.clear()
        self.set_document_ready(True)
        self._refresh()
        if restored:
            QTimer.singleShot(0, self._scroll_bottom)

    def set_busy(self, busy: bool) -> None:
        self._streaming = busy
        self.send_button.setEnabled(not busy and self._document_ready and bool(self.input.toPlainText().strip() or self._quote_text))
        self.input.setReadOnly(busy)
        self.clear_button.setEnabled(not busy)
        self.hint.setText("AI 正在回答…" if busy else "")
        self._refresh()

    # ------------------------------------------------------------------ quote
    def attach_quote(self, text: str, page: int) -> None:
        self._quote_text = text.strip()
        self._quote_page = page
        snippet = " ".join(self._quote_text.split())
        if len(snippet) > 88:
            snippet = snippet[:88] + "…"
        self.quote_label.setText(f"引用 · 第 {page} 页：{snippet}" if page else f"引用：{snippet}")
        self.quote_frame.show()
        self.input.setFocus()
        self._update_send_enabled()

    def clear_quote(self) -> None:
        self._quote_text = ""
        self._quote_page = 0
        self.quote_frame.hide()
        self._update_send_enabled()

    @property
    def quote_text(self) -> str:
        return self._quote_text

    @property
    def quote_page(self) -> int:
        return self._quote_page

    def _update_send_enabled(self) -> None:
        has_text = bool(self.input.toPlainText().strip()) or bool(self._quote_text)
        self.send_button.setEnabled(self._document_ready and has_text and not self._streaming)

    # ------------------------------------------------------------------- send
    def send(self) -> None:
        if self._streaming or not self._document_ready:
            return
        question = self.input.toPlainText().strip()
        quote = self._quote_text
        page = self._quote_page
        if not question:
            if quote:
                question = "请解释这段原文，并说明它在论文论证中的作用。"
            else:
                return
        self._last_request_history = [
            {"role": m.get("role", ""), "content": m.get("content", "")}
            for m in self.messages
            if m.get("role") in {"user", "assistant"} and m.get("content")
        ]
        self.messages.append(
            {
                "role": "user",
                "content": question,
                "quote": quote,
                "quote_page": page,
            }
        )
        self.input.clear()
        self.clear_quote()
        self._meta = ""
        self._streaming = True
        self.set_busy(True)
        self._refresh()
        self.ask_requested.emit(question, quote, page)

    def history_for_request(self) -> list[dict[str, str]]:
        return list(self._last_request_history)

    def begin_assistant(self) -> None:
        self.messages.append({"role": "assistant", "content": "", "meta": ""})
        self._stream_buffer = ""
        self._streaming = True
        self._refresh()

    def append_delta(self, text: str) -> None:
        if not self.messages or self.messages[-1].get("role") != "assistant":
            self.begin_assistant()
        self.messages[-1]["content"] = str(self.messages[-1].get("content", "")) + text
        self._render_timer.start()

    def _flush_stream(self) -> None:
        self._render(keep_bottom=True)

    def finish_assistant(self, meta: str = "", error: str = "") -> None:
        self._render_timer.stop()
        if not self.messages or self.messages[-1].get("role") != "assistant":
            self.begin_assistant()
        if error:
            current = str(self.messages[-1].get("content", ""))
            self.messages[-1]["content"] = (current + "\n\n" + error).strip()
        if meta:
            self.messages[-1]["meta"] = meta
            self._meta = meta
        self._streaming = False
        self.set_busy(False)
        self._refresh()

    def clear_conversation(self) -> None:
        if self._streaming:
            return
        self.messages = []
        self._meta = ""
        self._refresh()

    # ------------------------------------------------------------------- zoom
    def zoom_by(self, step: int) -> None:
        self.transcript.zoom_by(step)
        self._zoom_steps = max(-5, min(12, self._zoom_steps + step))
        font = QFont(self.input.font())
        font.setPointSizeF(max(8.0, 10.0 + self._zoom_steps * 0.55))
        self.input.setFont(font)
        self.zoom_changed.emit(self.zoom_percent)

    def reset_zoom(self) -> None:
        self.transcript.reset_zoom()
        self._zoom_steps = 0
        font = QFont(self.input.font())
        font.setPointSizeF(10.0)
        self.input.setFont(font)
        self.zoom_changed.emit(self.zoom_percent)

    @property
    def zoom_percent(self) -> int:
        return max(50, min(220, int(round(100 + self._zoom_steps * 8))))

    # ----------------------------------------------------------------- render
    def _refresh(self, keep_bottom: bool = False) -> None:
        html = render_chat_messages(
            self.messages,
            streaming=self._streaming,
            meta=self._meta,
            dark=theme_module.is_dark(),
        )
        self.transcript.set_html(html, keep_position=not keep_bottom)
        if keep_bottom:
            QTimer.singleShot(0, self._scroll_bottom)
        elif not self.messages:
            self._scroll_bottom()

    def _render(self, keep_bottom: bool = True) -> None:
        self._refresh(keep_bottom=keep_bottom)

    def _scroll_bottom(self) -> None:
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ------------------------------------------------------------------ events
    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
                event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                self.send()
                return True
            if event.key() == Qt.Key.Key_Escape and self._quote_text:
                self.clear_quote()
                return True
        return super().eventFilter(obj, event)
