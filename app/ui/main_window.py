from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..config import REASONING_LABELS, REASONING_OPTIONS, AppConfig
from ..llm import build_chat_context
from ..logging_setup import get_logger
from ..models import DocumentStructure
from ..ocr_client import OcrClient
from ..paths import EXPORT_DIR, HOME, TMP_DIR, app_home, resource_path
from ..pdf.cache import DocumentCache
from ..pdf.extractor import merge_ocr_pages
from ..review import apply_review_removals, review_candidates
from ..workers import ChatWorker, PrepareWorker, ReviewWorker, TranslationWorker
from . import theme as theme_module
from .chat_view import ChatView
from .pdf_view import PdfViewer
from .richtext import escape, render_document
from .settings_dialog import SettingsDialog
from .widgets import ZoomTextBrowser

log = get_logger("ui.main")


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, initial_pdf: Path | None = None):
        super().__init__()
        self.config = config
        self.pdf_path: Path | None = None
        self.document: DocumentStructure | None = None
        self.cache: DocumentCache | None = None
        self.translations: dict[str, str] = {}
        self._prepare_worker: PrepareWorker | None = None
        self._translation_worker: TranslationWorker | None = None
        self._review_worker: ReviewWorker | None = None
        self._chat_worker: ChatWorker | None = None
        self._retired: list = []
        self._translation_running = False
        self._review_running = False
        self._ocr_running = False
        self._chat_running = False
        self._translation_state = "idle"
        self._translation_note = ""
        self._translation_dirty = False
        self._pending_project = None
        self._closing = False
        self._current_page = 1
        self._page_total = 0
        self._middle_zoom = 1.0
        self._middle_is_web = os.environ.get("EASYPAPER_DISABLE_WEBENGINE") != "1"
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(400)
        self._render_timer.timeout.connect(self._flush_translation)

        self.ocr_client = OcrClient(self.config, self)
        self._connect_ocr_client()

        self.setWindowTitle("EasyPaper · 本地论文阅读翻译问答")
        icon_path = resource_path("easypaper.ico")
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setAcceptDrops(True)
        self.resize(self.config.ui.window_width, self.config.ui.window_height)
        self.setMinimumSize(1080, 700)
        self._build_ui()
        self._install_shortcuts()
        QTimer.singleShot(250, self._prewarm_ocr)
        self._update_actions()
        self._update_api_chip()
        self._update_state_chip()
        self._set_middle_html(render_document(None), reset=True)
        self.chat.set_document_ready(False)
        try:
            QApplication.styleHints().colorSchemeChanged.connect(self._on_system_color_scheme)
        except Exception:
            pass
        if initial_pdf:
            QTimer.singleShot(250, lambda: self.load_pdf(initial_pdf))

    def apply_appearance(self) -> None:
        self.chat.apply_appearance()
        self.pdf_view.apply_appearance()
        if self._middle_is_web and hasattr(self.translation_view, "apply_appearance"):
            self.translation_view.apply_appearance()
        elif not self._middle_is_web:
            c = theme_module.COLORS
            self.translation_view.setStyleSheet(
                f"QTextBrowser {{ background:{c.get('panel', '#FFFFFF')}; border:none; "
                f"color:{c.get('ink', '#20262E')}; }}"
            )
        if self.document is not None:
            self._render_translation()
        else:
            self._set_middle_html(render_document(None), reset=True)
        self.chat._refresh()
        self.update()

    def _on_system_color_scheme(self, *args) -> None:
        if self._closing or self.config.ui.appearance != "system":
            return
        theme_module.apply_theme(
            QApplication.instance(),
            "system",
            self.config.ui.color_theme,
        )
        self.apply_appearance()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 8)
        root.setSpacing(10)

        root.addWidget(self._build_topbar())

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(8)
        root.addWidget(self.splitter, 1)

        left_panel, left_body, left_header, left_footer = self._make_panel("原文 PDF", "")
        self.page_label = QLabel("第 0 / 0 页")
        self.page_label.setObjectName("Muted")
        left_header.addWidget(self.page_label, 0)
        self.pdf_view = PdfViewer(left_body)
        left_body.layout().addWidget(self.pdf_view, 1)
        self.pdf_view.visible_page_changed.connect(self._on_visible_page)
        self.pdf_view.zoom_changed.connect(self._on_pdf_zoom)
        self.pdf_view.status_message.connect(lambda msg: self._set_status(msg))
        self.pdf_view.quote_requested.connect(self._on_quote_requested)
        self.pdf_view.ask_requested.connect(self._on_ask_selection)
        self.left_zoom_label = QLabel("100%")
        self.left_zoom_label.setObjectName("Muted")
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("GearButton")
        self.settings_button.setText("⚙  设置")
        self.settings_button.setToolTip("OpenAI 接口、OCR 与翻译设置 (Ctrl+,)")
        self.settings_button.clicked.connect(self.open_settings)
        left_footer.addWidget(self.settings_button, 0)
        left_footer.addSpacing(6)
        left_footer.addStretch(1)
        left_footer.addWidget(self.left_zoom_label, 0)
        self.splitter.addWidget(left_panel)

        middle_panel, middle_body, middle_header, middle_footer = self._make_panel("全文翻译", "")
        self.translate_button = QPushButton("重新翻译")
        self.translate_button.setToolTip("忽略本地翻译缓存，重新调用 AI")
        self.translate_button.clicked.connect(self.retranslate)
        self.review_button = QPushButton("审查")
        self.review_button.setToolTip("调用 AI 整体审查译文，去除页眉、页脚等冗余信息")
        self.review_button.clicked.connect(
            lambda: self._maybe_start_review(force=True, manual=True)
        )
        self.export_button = QPushButton("导出")
        self.export_button.setToolTip("导出 Markdown 译文")
        self.export_button.clicked.connect(self.export_markdown)
        self.middle_state_chip = QLabel("未打开")
        self.middle_state_chip.setObjectName("Chip")
        middle_header.addWidget(self.middle_state_chip, 0)
        middle_header.addWidget(self.review_button, 0)
        middle_header.addWidget(self.translate_button, 0)
        middle_header.addWidget(self.export_button, 0)
        if self._middle_is_web:
            try:
                from .web_translation import TranslationWebView

                self.translation_view = TranslationWebView(middle_body)
                self.translation_view.zoom_changed.connect(self._on_middle_web_zoom)
                self.translation_view.load_failed.connect(
                    lambda msg: self._set_status(msg)
                )
            except Exception:
                log.exception("QtWebEngine unavailable, falling back to QTextBrowser")
                self._middle_is_web = False
        if not self._middle_is_web:
            self.translation_view = ZoomTextBrowser(middle_body)
            self.translation_view.external_zoom = True
            c = theme_module.COLORS
            self.translation_view.setStyleSheet(
                f"QTextBrowser {{ background:{c.get('panel', '#FFFFFF')}; border:none; "
                f"color:{c.get('ink', '#20262E')}; }}"
            )
            self.translation_view.ctrl_wheel.connect(self._on_middle_zoom)
        middle_body.layout().addWidget(self.translation_view, 1)
        self.translation_status = QLabel("等待打开 PDF")
        self.translation_status.setObjectName("Muted")
        self.translation_status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.translation_progress = QProgressBar()
        self.translation_progress.setFixedWidth(110)
        self.translation_progress.setFixedHeight(6)
        self.translation_progress.setTextVisible(False)
        self.translation_progress.hide()
        self.middle_zoom_label = QLabel("100%")
        self.middle_zoom_label.setObjectName("Muted")
        middle_footer.addWidget(self.translation_status, 1)
        middle_footer.addWidget(self.translation_progress, 0)
        middle_footer.addSpacing(8)
        middle_footer.addWidget(self.middle_zoom_label, 0)
        self.splitter.addWidget(middle_panel)

        right_panel, right_body, right_header, right_footer = self._make_panel("AI 问答", "")
        self.new_chat_button = QPushButton("新对话")
        self.new_chat_button.clicked.connect(self._new_chat)
        right_header.addWidget(self.new_chat_button, 0)
        chat_thinking_label = QLabel("思考")
        chat_thinking_label.setObjectName("Muted")
        right_header.addWidget(chat_thinking_label, 0)
        self.chat_thinking = QComboBox()
        for label, value in REASONING_OPTIONS:
            self.chat_thinking.addItem(label, value)
        self.chat_thinking.setFixedWidth(104)
        self.chat_thinking.setToolTip("AI 对话的模型思考强度；强度越高通常越慢、越贵")
        index = self.chat_thinking.findData(self.config.chat.reasoning_effort)
        self.chat_thinking.setCurrentIndex(index if index >= 0 else 0)
        self.chat_thinking.currentIndexChanged.connect(self._on_chat_thinking_changed)
        right_header.addWidget(self.chat_thinking, 0)
        self.right_zoom_label = QLabel("100%")
        self.right_zoom_label.setObjectName("Muted")
        right_header.addWidget(self.right_zoom_label, 0)
        self.chat = ChatView(right_body)
        self.chat.ask_requested.connect(self._on_ask_requested)
        self.chat.zoom_changed.connect(self._on_chat_zoom)
        right_body.layout().addWidget(self.chat, 1)
        footer_widget = right_footer.parentWidget()
        if footer_widget is not None:
            footer_widget.hide()
        self.splitter.addWidget(right_panel)
        self.splitter.setStretchFactor(0, 38)
        self.splitter.setStretchFactor(1, 38)
        self.splitter.setStretchFactor(2, 24)
        self.splitter.setSizes(self.config.ui.splitter_sizes)

        self._build_statusbar()

    def _build_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("HeaderBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)
        icon_label = QLabel()
        icon_path = resource_path("easypaper.ico")
        if icon_path.is_file():
            pix = QPixmap(str(icon_path)).scaled(
                38,
                38,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            icon_label.setPixmap(pix)
        else:
            icon_label.setText("EP")
            icon_label.setStyleSheet("font-weight:800; font-size:15pt; color:#0F766E;")
        layout.addWidget(icon_label, 0)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        app_title = QLabel("EasyPaper")
        app_title.setObjectName("AppTitle")
        self.doc_title = QLabel("本地论文阅读 · 全文翻译 · AI 问答")
        self.doc_title.setObjectName("AppSubtitle")
        self.doc_title.setMaximumWidth(760)
        titles.addWidget(app_title)
        titles.addWidget(self.doc_title)
        layout.addLayout(titles, 1)

        self.api_chip = QLabel("API 未配置")
        self.api_chip.setObjectName("WarmChip")
        self.api_chip.setToolTip("点击左下角齿轮配置 OpenAI 兼容接口")
        layout.addWidget(self.api_chip, 0)
        self.open_button = QPushButton("打开 PDF")
        self.open_button.setObjectName("Primary")
        self.open_button.setToolTip("打开本地 PDF (Ctrl+O)")
        self.open_button.clicked.connect(self.open_pdf_dialog)
        layout.addWidget(self.open_button, 0)
        self.reparse_button = QPushButton("重新解析")
        self.reparse_button.setToolTip("重新执行 PDF 结构化解析 / OCR，可保留已有译文缓存")
        self.reparse_button.clicked.connect(self.reparse)
        layout.addWidget(self.reparse_button, 0)
        self.import_project_button = QPushButton("导入项目")
        self.import_project_button.setToolTip("导入包含 PDF、译文、图片和问答记录的 EasyPaper 项目包")
        self.import_project_button.clicked.connect(self.import_project)
        layout.addWidget(self.import_project_button, 0)
        self.export_project_button = QPushButton("导出项目")
        self.export_project_button.setToolTip("导出 PDF、译文、公式/表格图片和问答记录")
        self.export_project_button.clicked.connect(self.export_project)
        layout.addWidget(self.export_project_button, 0)
        return bar

    def _make_panel(self, title: str, hint: str):
        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)
        header = QFrame()
        header.setObjectName("PanelHeader")
        header.setFixedHeight(48)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 6, 10, 6)
        header_layout.setSpacing(6)
        title_label = QLabel(title)
        title_label.setObjectName("PanelTitle")
        header_layout.addWidget(title_label, 0)
        hint_label = QLabel(hint)
        hint_label.setObjectName("PanelHint")
        hint_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        header_layout.addWidget(hint_label, 1)
        if not hint:
            hint_label.hide()
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        footer = QFrame()
        footer.setObjectName("FooterBar")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(10, 5, 10, 5)
        footer_layout.setSpacing(6)
        panel_layout.addWidget(header, 0)
        panel_layout.addWidget(body, 1)
        panel_layout.addWidget(footer, 0)
        return panel, body, header_layout, footer_layout

    def _build_statusbar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        self.status_message = QLabel("就绪")
        self.status_message.setObjectName("Muted")
        bar.addWidget(self.status_message, 1)
        self.cache_label = QLabel(f"缓存 {HOME / 'cache'}")
        self.cache_label.setObjectName("Muted")
        bar.addPermanentWidget(self.cache_label, 0)
        self.status_progress = QProgressBar()
        self.status_progress.setFixedWidth(150)
        self.status_progress.setFixedHeight(6)
        self.status_progress.setTextVisible(False)
        self.status_progress.hide()
        bar.addPermanentWidget(self.status_progress, 0)
        self.api_status = QLabel("API: 未配置")
        self.api_status.setObjectName("Muted")
        bar.addPermanentWidget(self.api_status, 0)

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.open_pdf_dialog)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self.open_settings)
        QShortcut(QKeySequence("Ctrl+0"), self, activated=self.reset_zoom)
        QShortcut(QKeySequence("F5"), self, activated=self.reparse)

    # ------------------------------------------------------------- PDF loading
    def open_pdf_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开 PDF",
            str(HOME),
            "PDF 文件 (*.pdf);;所有文件 (*.*)",
        )
        if path:
            self.load_pdf(Path(path))

    def load_pdf(self, path: Path | str) -> None:
        path = Path(path).expanduser()
        if not path.is_file():
            QMessageBox.warning(self, "无法打开", f"文件不存在：\n{path}")
            return
        if path.suffix.lower() != ".pdf":
            QMessageBox.warning(self, "无法打开", "请选择 PDF 文件。")
            return
        self._cancel_active_tasks()
        self.pdf_path = path.resolve()
        self.document = None
        self.cache = None
        self.translations = {}
        self._translation_running = False
        self._translation_state = "idle"
        self._translation_note = ""
        self.pdf_view.clear_document()
        self.chat.reset_document()
        self._set_middle_html(
            render_document(None, message="正在读取 PDF、计算指纹并生成结构化缓存…"),
            reset=True,
        )
        self.translation_status.setText("正在准备 PDF…")
        self.doc_title.setText(self.pdf_path.name)
        self.setWindowTitle(f"{self.pdf_path.stem} · EasyPaper")
        self.page_label.setText("第 0 / 0 页")
        self._update_actions()
        self._update_state_chip()
        self._begin_prepare(force_reparse=False)

    def _restore_pending_project(self) -> None:
        bundle = self._pending_project
        self._pending_project = None
        if bundle is None or self.document is None or self.cache is None:
            return
        source_blocks = getattr(bundle, "structure", None)
        imported = getattr(source_blocks, "blocks", []) if source_blocks is not None else []
        by_id = {block.id: block for block in imported}
        by_hash: dict[str, list] = {}
        for block in imported:
            digest = hashlib.sha256(block.text.encode("utf-8", "ignore")).hexdigest()
            by_hash.setdefault(digest, []).append(block)
        translations: dict[str, str] = {}
        for block in self.document.blocks:
            digest = hashlib.sha256(block.text.encode("utf-8", "ignore")).hexdigest()
            candidate = by_id.get(block.id)
            if candidate is None or candidate.text != block.text:
                choices = by_hash.get(digest, [])
                candidate = choices[0] if len(choices) == 1 else None
            if candidate is None:
                continue
            value = str(bundle.translations.get(candidate.id, "")).strip()
            if value:
                block.translation = value
                translations[block.id] = value
            block.hidden = bool(candidate.hidden)
        if source_blocks is not None:
            self.document.review_summary = dict(getattr(source_blocks, "review_summary", {}) or {})
        self.document.apply_translations(translations)
        self.translations = dict(self.cache.load_translation(self.document) or {})
        missing = [
            block
            for block in self.document.translatable_blocks()
            if block.text.strip() and block.id not in translations
        ]
        complete = bool(getattr(bundle, "complete", False)) and not missing
        self.cache.save_translation(self.document, translations, complete=complete)
        try:
            self.cache.save_structure(self.document)
        except Exception:
            log.debug("could not save imported project structure", exc_info=True)
        self.chat.restore_messages(getattr(bundle, "chat", []) or [])

    def _begin_prepare(self, force_reparse: bool) -> None:
        if self.pdf_path is None:
            return
        worker = PrepareWorker(self.pdf_path, self.config, force_reparse=force_reparse)
        self._prepare_worker = worker
        worker.progress.connect(self._on_prepare_progress)
        worker.ready.connect(self._on_prepare_ready)
        worker.failed.connect(self._on_prepare_failed)
        worker.finished.connect(self._on_prepare_finished)
        self._set_busy_status("正在解析 PDF…")
        self._show_progress(True, 0, 1)
        worker.start()

    def _on_prepare_progress(self, phase: str, current: int, total: int, message: str) -> None:
        self._set_status(message)
        self.translation_status.setText(message)
        self._show_progress(total > 1, current, total)

    def _on_prepare_ready(self, document: DocumentStructure, cache: DocumentCache, targets: list) -> None:
        if self._closing:
            return
        self.document = document
        self.cache = cache
        self._show_progress(False)
        self.pdf_view.set_document(Path(document.source_path), document)
        self.page_label.setText(f"第 1 / {document.page_count} 页")
        self._page_total = document.page_count
        self._current_page = 1
        self.doc_title.setText(document.title[:120] if document.title else document.file_name)
        self.setWindowTitle(f"{document.title[:80]} · EasyPaper")
        self.chat.reset_document()
        self.chat.set_document_ready(True)
        if self._pending_project is not None:
            self._restore_pending_project()
        stats = document.stats()
        self.cache_label.setText(f"缓存 {cache.root.name} · {stats['blocks']} 块")
        self._set_status(
            f"已载入：{document.page_count} 页 · {stats['translatable']} 个可译块 · "
            f"{stats['tables']} 表格 · {stats['formulas']} 公式"
        )
        self._update_actions()
        if targets:
            self._start_ocr(targets)
        else:
            self._load_translation_and_start()

    def _on_prepare_failed(self, message: str) -> None:
        self._show_progress(False)
        if message == "已取消":
            return
        self._set_status(f"解析失败：{message}")
        self.translation_status.setText(f"解析失败：{message}")
        if not self._closing:
            QMessageBox.critical(self, "PDF 解析失败", message)
        self._update_actions()

    def _on_prepare_finished(self) -> None:
        worker = self._prepare_worker
        self._prepare_worker = None
        if worker is not None:
            worker.deleteLater()

    def _on_ocr_ready(self) -> None:
        pass

    # ------------------------------------------------------------------- OCR
    def _connect_ocr_client(self) -> None:
        self.ocr_client.starting.connect(self._on_ocr_starting)
        self.ocr_client.progress.connect(self._on_ocr_progress)
        self.ocr_client.finished.connect(self._on_ocr_finished)
        self.ocr_client.failed.connect(self._on_ocr_failed)
        self.ocr_client.cancelled.connect(self._on_ocr_cancelled)
        self.ocr_client.prewarm_failed.connect(self._on_ocr_prewarm_failed)

    def _on_ocr_prewarm_failed(self, message: str) -> None:
        log.info("OCR preload skipped: %s", message)
        self._set_status("OCR 预加载未完成；下次识别扫描页时会再次尝试。")

    def _prewarm_ocr(self) -> None:
        if self._closing or not self.config.ocr.enabled or not self.config.ocr.prewarm:
            return
        if self.config.ocr.mode == "off" or self._ocr_running:
            return
        self.ocr_client.config = self.config
        self.ocr_client.prewarm()

    def _start_ocr(self, targets: list[int]) -> None:
        if not self.document or not self.pdf_path:
            return
        self._ocr_running = True
        self._translation_state = "ocr"
        self._translation_note = f"正在使用本地 PaddleOCR-VL 预处理 {len(targets)} 个扫描页…"
        self._update_state_chip()
        self._update_actions()
        self.translation_status.setText(self._translation_note)
        self._set_middle_html(
            render_document(
                self.document,
                "running",
                self.config.llm.model,
                self._translation_note,
                asset_base=self.cache.root if self.cache else None,
                zoom=1.0 if self._middle_is_web else self._middle_zoom,
            ),
            reset=True,
        )
        self._set_busy_status(self._translation_note)
        self._show_progress(True, 0, len(targets))
        self.ocr_client.config = self.config
        self.ocr_client.ocr_document(self.pdf_path, targets)

    def _on_ocr_starting(self, message: str) -> None:
        self._set_status(message)
        self.translation_status.setText(message)

    def _on_ocr_progress(self, stage: str, current: int, total: int, message: str) -> None:
        self._set_status(message)
        self.translation_status.setText(message)
        if total:
            self._show_progress(True, current, total)
        if self.document and stage in {"ocr", "infer", "loading"}:
            self._translation_note = message

    def _on_ocr_finished(self, request_id: int, pages: dict, engine: str, elapsed: float) -> None:
        self._ocr_running = False
        self._show_progress(False)
        if not self.document or not self.cache:
            return
        if pages:
            self.cache.invalidate_signature()
            merge_ocr_pages(self.document, pages, engine or "PaddleOCR-VL", elapsed)
            self.cache.save_structure(self.document)
            self._set_status(
                f"本地 OCR 完成：{len(pages)} 页 · {engine} · {elapsed:.1f}s；"
                "已写入结构化缓存"
            )
            self.translation_status.setText(f"OCR 完成（{len(pages)} 页），开始翻译…")
        else:
            self._set_status("OCR 未返回文字，继续使用 PDF 文字层。")
        self._update_actions()
        self._load_translation_and_start()

    def _on_ocr_failed(self, message: str) -> None:
        self._ocr_running = False
        self._show_progress(False)
        self._set_status(f"本地 OCR 不可用：{message}")
        self.translation_status.setText("OCR 不可用，已继续使用 PDF 文字层")
        if self.document and self.cache:
            self.document.ocr_summary.update({"status": "failed", "error": message[:500]})
            try:
                self.cache.save_structure(self.document)
            except Exception:
                log.debug("cannot save OCR failure state", exc_info=True)
        self._translation_note = f"本地 OCR 不可用，已使用 PDF 文字层。{message}"
        self._update_actions()
        self._load_translation_and_start()

    def _on_ocr_cancelled(self) -> None:
        self._ocr_running = False
        self._show_progress(False)
        self._set_status("OCR 已取消")

    # ----------------------------------------------------------- translation
    def _load_translation_and_start(self) -> None:
        if not self.document or not self.cache:
            return
        self.cache.invalidate_signature()
        cached = self.cache.load_translation(self.document) or {}
        self.translations = dict(cached)
        self.document.apply_translations(self.translations)
        translatable = [b for b in self.document.translatable_blocks() if b.text.strip()]
        missing = [b for b in translatable if b.id not in self.translations]
        self._update_actions()
        if not translatable:
            self._translation_state = "done"
            self._translation_note = "未发现需要翻译的正文块。"
            self._update_state_chip()
            self.translation_status.setText(self._translation_note)
            self._render_translation(reset=True)
            return
        if not missing:
            meta = self.cache.translation_meta(self.document) or {}
            if meta.get("complete"):
                note = f"已载入本地翻译缓存（{len(self.translations)} 块）"
            else:
                note = f"已载入部分翻译缓存（{len(self.translations)} 块）"
            self._translation_state = "done" if meta.get("complete", True) else "partial"
            self._translation_note = note
            self._update_state_chip()
            self.translation_status.setText(note)
            self._render_translation(reset=True)
            self._set_status(note)
            if meta.get("complete", True):
                QTimer.singleShot(120, self._maybe_start_review)
            return
        if not self.config.api_ready:
            self._translation_state = "error"
            self._translation_note = (
                "尚未配置 AI 接口：点击左下角“设置”填写 base_url、API Key 与模型，"
                "然后点击“重新翻译”。"
            )
            self._update_state_chip()
            self.translation_status.setText("等待配置 AI 接口")
            self._render_translation(reset=True)
            self._set_status("请先配置 OpenAI 兼容接口")
            return
        self._start_translation(force=False)

    def _start_translation(self, force: bool) -> None:
        if not self.document or not self.cache:
            return
        if self._translation_running:
            return
        if not self.config.api_ready:
            self._translation_state = "error"
            self._translation_note = "API 未配置，无法翻译。请打开左下角设置。"
            self._update_state_chip()
            self._render_translation()
            self.open_settings()
            return
        if force:
            self.cache.clear_translation(self.document)
            self.cache.invalidate_signature()
            for block in self.document.blocks:
                block.translation = ""
                block.hidden = False
            self.document.review_summary = {}
            self.translations = {}
            self.document.apply_translations({})
        self._translation_running = True
        self._translation_state = "running"
        self._translation_note = "正在调用 AI 分块翻译，界面可继续阅读和提问…"
        self._update_state_chip()
        self._update_actions()
        self.translation_status.setText(self._translation_note)
        self.translation_progress.show()
        self.translation_progress.setRange(0, 0)
        self._show_progress(False)
        self._render_translation()
        worker = TranslationWorker(
            self.document,
            self.cache,
            self.config,
            existing=dict(self.translations),
            parent=self,
        )
        self._translation_worker = worker
        worker.progress.connect(self._on_translation_progress)
        worker.partial.connect(self._on_translation_partial)
        worker.finished_ok.connect(self._on_translation_finished)
        worker.failed.connect(self._on_translation_failed)
        worker.finished.connect(self._on_translation_worker_finished)
        worker.start()
        self._set_status("AI 翻译已开始…")

    def _on_translation_progress(self, done: int, total: int, message: str) -> None:
        self._translation_note = message
        self.translation_status.setText(message)
        self._set_status(message)
        self.translation_progress.setRange(0, max(1, total))
        self.translation_progress.setValue(done)
        self.translation_progress.show()
        self.middle_state_chip.setText(f"翻译中 {done}/{total}")

    def _on_translation_partial(self, mapping: dict) -> None:
        self.translations.update(mapping)
        if self.document:
            self.document.apply_translations(mapping)
        self._translation_dirty = True
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _flush_translation(self) -> None:
        if not self._translation_dirty:
            return
        self._translation_dirty = False
        if self.document:
            self.document.apply_translations(self.translations)
        self._render_translation()

    def _on_translation_finished(self, translations: dict, complete: bool, note: str) -> None:
        self.translations = dict(translations)
        if self.document:
            self.document.apply_translations(self.translations)
        self._translation_running = False
        self._translation_state = "done" if complete else "partial"
        self._translation_note = note
        self.translation_progress.hide()
        self._update_state_chip()
        self._update_actions()
        self.translation_status.setText(note)
        self._render_translation()
        self._set_status(f"{note} · {len(self.translations)} 块译文已缓存")
        if not complete:
            QMessageBox.warning(
                self,
                "部分翻译未完成",
                note + "\n\n已成功的译文已保存；网络或额度恢复后点击“重新翻译”会继续补齐。",
            )
            return
        if self.config.translation.review_pass:
            QTimer.singleShot(120, self._maybe_start_review)

    def _on_translation_failed(self, message: str, translations: dict) -> None:
        if translations:
            self.translations.update(translations)
        if self.document:
            self.document.apply_translations(self.translations)
        self._translation_running = False
        self._translation_state = "error"
        self._translation_note = f"翻译中断：{message}"
        self.translation_progress.hide()
        self._update_state_chip()
        self._update_actions()
        self.translation_status.setText("翻译中断")
        self._render_translation()
        self._set_status(f"翻译失败：{message}")

    def _on_translation_worker_finished(self) -> None:
        worker = self._translation_worker
        self._translation_worker = None
        if worker is not None:
            worker.deleteLater()

    def _maybe_start_review(self, force: bool = False, manual: bool = False) -> None:
        if not self.document or not self.cache:
            return
        if self._review_running or self._translation_running or self._ocr_running:
            return
        if (not self.config.translation.review_pass and not manual) or not self.config.api_ready:
            return
        summary = self.document.review_summary or {}
        if not force and summary.get("status") in {"done", "failed"}:
            return
        translatable = [b for b in self.document.translatable_blocks() if b.text.strip()]
        if not translatable or any(b.id not in self.translations for b in translatable):
            return
        if force:
            for block in self.document.blocks:
                block.hidden = False
            self.document.review_summary = {}
        candidates = review_candidates(self.document)
        if not candidates:
            self.document.review_summary = {
                "status": "done",
                "removed": 0,
                "removed_ids": [],
            }
            try:
                self.cache.save_structure(self.document)
            except Exception:
                log.debug("could not save review summary", exc_info=True)
            self._translation_note = "AI 审查：未发现页眉、页脚等冗余信息"
            self._render_translation()
            return
        worker = ReviewWorker(
            self.config,
            self.document,
            candidates,
            parent=self,
        )
        self._review_worker = worker
        self._review_running = True
        self.review_button.setText("审查中…")
        self.review_button.setEnabled(False)
        self._update_actions()
        self.translation_status.setText(f"AI 整体审查中（{len(candidates)} 个候选块）…")
        self._set_status("正在调用 AI 审查译文并清理页面冗余信息…")
        worker.finished_ok.connect(self._on_review_finished)
        worker.failed.connect(self._on_review_failed)
        worker.finished.connect(self._on_review_worker_finished)
        worker.start()

    def _on_review_finished(self, removed: list, meta: dict) -> None:
        self._review_running = False
        if not self.document or not self.cache:
            return
        count = apply_review_removals(self.document, removed)
        try:
            self.cache.save_structure(self.document)
        except Exception:
            log.debug("could not save review result", exc_info=True)
        if count:
            note = f"AI 审查完成：移除 {count} 个页眉/页脚等冗余块"
        else:
            note = "AI 审查完成：未发现需要删除的冗余信息"
        self._translation_note = note
        self.translation_status.setText(note)
        self._set_status(note)
        self._render_translation()
        self._update_actions()

    def _on_review_failed(self, message: str) -> None:
        self._review_running = False
        if self.document and self.cache:
            self.document.review_summary = {"status": "failed", "error": message[:500]}
            try:
                self.cache.save_structure(self.document)
            except Exception:
                log.debug("could not save review failure", exc_info=True)
        self._translation_note = f"AI 审查失败：{message}（译文已保留）"
        self.translation_status.setText("AI 审查失败，译文已保留")
        self._set_status(f"AI 审查失败：{message}")
        self._render_translation()
        self._update_actions()

    def _on_review_worker_finished(self) -> None:
        worker = self._review_worker
        self._review_worker = None
        if worker is not None:
            worker.deleteLater()
        if not self._review_running:
            self.review_button.setText("审查")
        self._update_actions()

    def retranslate(self) -> None:
        if not self.document:
            return
        if self._translation_running or self._review_running:
            return
        if not self.config.api_ready:
            self.open_settings()
            return
        answer = QMessageBox.question(
            self,
            "重新翻译",
            "将清除本文档的翻译缓存并重新调用 AI 翻译全文。\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_translation(force=True)

    def _set_middle_html(self, html: str, reset: bool = False) -> None:
        base = self.cache.root if self.cache else TMP_DIR
        if self._middle_is_web:
            self.translation_view.set_html(html, base, keep_position=not reset)
        else:
            self.translation_view.set_html(html, keep_position=not reset)

    def _render_translation(self, reset: bool = False) -> None:
        if not self.document:
            self._set_middle_html(render_document(None), reset=reset)
            return
        status = "running" if (self._translation_running or self._ocr_running) else "done"
        self._set_middle_html(
            render_document(
                self.document,
                status,
                self.config.llm.model,
                self._translation_note,
                asset_base=self.cache.root if self.cache else None,
                zoom=1.0 if self._middle_is_web else self._middle_zoom,
            ),
            reset=reset,
        )

    # --------------------------------------------------------------- settings
    def open_settings(self) -> None:
        if self._ocr_running:
            QMessageBox.information(self, "OCR 进行中", "请等待本地 OCR 完成后再修改设置。")
            return
        dialog = SettingsDialog(self.config, self)
        dialog.saved.connect(self._on_settings_saved)
        dialog.exec()

    def _on_settings_saved(self, config: AppConfig) -> None:
        old_config = self.config
        self.config = config
        if QApplication.instance() is not None:
            theme_module.apply_theme(
                QApplication.instance(),
                config.ui.appearance,
                config.ui.color_theme,
            )
            self.apply_appearance()
        self._update_api_chip()
        # Recreate the sidecar client when its paths/device changed.
        try:
            self.ocr_client.shutdown()
            self.ocr_client.deleteLater()
        except Exception:
            pass
        self.ocr_client = OcrClient(self.config, self)
        self._connect_ocr_client()
        QTimer.singleShot(300, self._prewarm_ocr)
        if self.document:
            if self._translation_state in {"error", "idle"} and not self._translation_running:
                self._translation_note = "设置已保存，正在尝试翻译…"
                self._load_translation_and_start()
            else:
                self.translation_status.setText("设置已保存")
                self._set_status("设置已保存（模型变化后请点击“重新翻译”以使用新模型）")
                if self.config.translation.review_pass and self._translation_state == "done":
                    QTimer.singleShot(100, self._maybe_start_review)
        else:
            self._set_status("设置已保存")

    # -------------------------------------------------------------------- OCR
    def reparse(self) -> None:
        if self.pdf_path is None:
            return
        if self._ocr_running or self._translation_running or self._review_running:
            QMessageBox.information(self, "请稍候", "当前任务尚未结束，请稍后再重新解析。")
            return
        self._cancel_translation()
        self._cancel_review()
        self.document = None
        self.cache = None
        self.chat.reset_document()
        self.translation_state_reset()
        self._begin_prepare(force_reparse=True)
        self._set_status("正在重新解析 PDF…")

    def translation_state_reset(self) -> None:
        self.translations = {}
        self._translation_state = "idle"
        self._translation_note = "正在重新解析…"
        self._update_state_chip()

    # ----------------------------------------------------------------- export
    def export_markdown(self) -> None:
        if not self.document:
            return
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        default = EXPORT_DIR / f"{self.pdf_path.stem if self.pdf_path else 'paper'}-译文.md"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Markdown", str(default), "Markdown (*.md);;所有文件 (*.*)"
        )
        if not path:
            return
        target = Path(path)
        try:
            target.write_text(self._document_markdown(), encoding="utf-8")
            self._set_status(f"已导出：{target}")
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def export_project(self) -> None:
        if not self.document or not self.cache or not self.pdf_path:
            QMessageBox.information(self, "导出项目", "请先打开一篇 PDF。")
            return
        from ..project_package import export_project as export_project_package

        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        default = EXPORT_DIR / f"{self.pdf_path.stem if self.pdf_path else 'paper'}-项目.zip"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出项目",
            str(default),
            "EasyPaper 项目包 (*.zip);;所有文件 (*.*)",
        )
        if not path:
            return
        target = Path(path)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = export_project_package(
                self.document,
                self.config,
                self.cache,
                self.pdf_path,
                self.chat.messages,
                target,
                markdown=self._document_markdown(),
            )
            self._set_status(
                f"已导出项目：{result.path.name} · {result.translations} 块译文 · {result.assets} 张图片"
            )
            QMessageBox.information(
                self,
                "导出完成",
                f"项目包已写入：\n{result.path}\n\n"
                f"PDF + 结构块：{result.blocks}\n译文：{result.translations}\n"
                f"图片：{result.assets}\n大小：{result.bytes / 1024 / 1024:.1f} MB",
            )
        except Exception as exc:
            QMessageBox.critical(self, "导出项目失败", str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def import_project(self) -> None:
        from ..project_package import ProjectError, extract_project, prepare_project_cache

        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入项目",
            str(EXPORT_DIR),
            "EasyPaper 项目包 (*.zip);;所有文件 (*.*)",
        )
        if not path:
            return
        if self.document is not None:
            answer = QMessageBox.question(
                self,
                "导入项目",
                "导入项目会关闭当前论文，并载入项目包中的原文、译文、图片和问答记录。是否继续？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            bundle = extract_project(Path(path))
            prepare_project_cache(bundle, self.config)
            self._pending_project = bundle
            self.load_pdf(bundle.pdf_path)
            self._set_status("项目已导入，正在自动载入原文、译文和问答记录…")
        except ProjectError as exc:
            self._pending_project = None
            QMessageBox.critical(self, "导入项目失败", str(exc))
        except Exception as exc:
            self._pending_project = None
            QMessageBox.critical(self, "导入项目失败", f"{type(exc).__name__}: {exc}")
        finally:
            QApplication.restoreOverrideCursor()

    def _document_markdown(self) -> str:
        doc = self.document
        if not doc:
            return ""
        lines = [f"# {doc.title}", ""]
        if doc.authors:
            lines.extend([f"> {doc.authors}", ""])
        lines.extend(
            [
                f"> 来源：`{doc.file_name}` · {doc.page_count} 页 · 由 EasyPaper 生成",
                f"> 模型：`{self.config.llm.model}` · 目标语言：{self.config.translation.target_language}",
                "",
            ]
        )
        current_page = None
        for block in doc.blocks:
            if block.hidden:
                continue
            if block.page != current_page:
                current_page = block.page
                lines.extend(["", f"---", "", f"## 第 {current_page} 页", ""])
            text = block.translation.strip() or block.text.strip()
            if not text:
                continue
            if block.kind == "title":
                lines.extend([f"# {text}", ""])
            elif block.kind == "heading":
                prefix = "#" * min(6, 2 + max(0, block.level))
                lines.extend([f"{prefix} {text}", ""])
            elif block.kind == "table":
                lines.extend([text, ""])
            elif block.kind == "formula":
                lines.extend(["$$", block.text.strip(), "$$", ""])
            elif block.kind == "caption":
                lines.extend([f"**{block.display_label}：** {text}", ""])
            elif block.kind == "reference":
                lines.extend([f"- {block.text.strip()}", ""])
            else:
                lines.extend([text, ""])
        if self._translation_note:
            lines.extend(["", f"> 翻译状态：{self._translation_note}"])
        return "\n".join(lines).strip() + "\n"

    # ------------------------------------------------------------------- chat
    def _on_chat_thinking_changed(self) -> None:
        value = self.chat_thinking.currentData() or "auto"
        if value == self.config.chat.reasoning_effort:
            return
        self.config.chat.reasoning_effort = value
        self.config.save()
        self._set_status(f"AI 对话思考强度：{REASONING_LABELS.get(value, value)}")

    def _new_chat(self) -> None:
        if self._chat_running:
            return
        self.chat.clear_conversation()
        self.chat.clear_quote()

    def _on_quote_requested(self, text: str, page: int) -> None:
        self.chat.attach_quote(text, page)
        self.chat.input.setFocus()
        self._set_status("已引用选中原文，请输入问题后发送")
        self._focus_right_panel()

    def _on_ask_selection(self, text: str, page: int) -> None:
        self.chat.attach_quote(text, page)
        if not self.chat.input.toPlainText().strip():
            self.chat.input.setPlainText("请解释这段原文，并说明它在论文中的作用。")
        self.chat.send()
        self._focus_right_panel()

    def _focus_right_panel(self) -> None:
        self.chat.input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.chat.raise_()

    def _on_ask_requested(self, question: str, quote: str, page: int) -> None:
        if not self.document:
            self.chat.finish_assistant(error="请先打开一篇 PDF。")
            return
        if self._chat_running:
            return
        if not self.config.api_ready:
            self.chat.begin_assistant()
            self.chat.finish_assistant(
                error="尚未配置 AI 接口。请点击左下角“设置”填写 base_url、API Key 与模型后重试。"
            )
            self.open_settings()
            return
        history = self.chat.history_for_request()
        worker = ChatWorker(
            self.config,
            self.document,
            question,
            history,
            selected_text=quote,
            selected_page=page,
            parent=self,
        )
        self._chat_worker = worker
        self._chat_running = True
        worker.delta.connect(self.chat.append_delta)
        worker.finished_ok.connect(self._on_chat_finished)
        worker.failed.connect(self._on_chat_failed)
        worker.finished.connect(self._on_chat_worker_finished)
        self.chat.begin_assistant()
        self._focus_right_panel()
        self._set_status("AI 正在基于全文回答…")
        worker.start()

    def _on_chat_finished(self, answer: str, meta: dict) -> None:
        self._chat_running = False
        parts = []
        if meta.get("retrieval"):
            parts.append(f"全文检索 {meta.get('used', 0)}/{meta.get('chunks', 0)} 段")
        else:
            parts.append("基于全文")
        if meta.get("cancelled"):
            parts.append("已停止")
        if meta.get("elapsed"):
            parts.append(f"{meta['elapsed']}s")
        if meta.get("tokens_est"):
            parts.append(f"约 {meta['tokens_est']} tokens")
        self.chat.finish_assistant(meta=" · ".join(parts))
        self._set_status("回答完成")

    def _on_chat_failed(self, message: str, meta: dict) -> None:
        self._chat_running = False
        self.chat.finish_assistant(error=f"请求失败：{message}")
        self._set_status(f"问答失败：{message}")

    def _on_chat_worker_finished(self) -> None:
        worker = self._chat_worker
        self._chat_worker = None
        if worker is not None:
            worker.deleteLater()
        self._chat_running = False

    # --------------------------------------------------------------- zoom/ui
    def _on_pdf_zoom(self, percent: int) -> None:
        self.left_zoom_label.setText(f"{percent}%")

    def _on_middle_zoom(self, step: int) -> None:
        if self.document is None or self._middle_is_web:
            return
        factor = 1.15 if step > 0 else 1 / 1.15
        self._middle_zoom = max(0.5, min(2.5, self._middle_zoom * factor))
        self.middle_zoom_label.setText(f"{int(round(self._middle_zoom * 100))}%")
        self._render_translation()

    def _on_middle_web_zoom(self, percent: int) -> None:
        self._middle_zoom = percent / 100.0
        self.middle_zoom_label.setText(f"{percent}%")

    def _on_chat_zoom(self, percent: int) -> None:
        self.right_zoom_label.setText(f"{percent}%")

    def reset_zoom(self) -> None:
        self.pdf_view.reset_zoom()
        self._middle_zoom = 1.0
        self.translation_view.reset_zoom()
        self.chat.reset_zoom()
        self.middle_zoom_label.setText("100%")
        self.right_zoom_label.setText("100%")
        if self.document is not None and not self._middle_is_web:
            self._render_translation()

    def _on_visible_page(self, page: int, total: int) -> None:
        self._current_page = page
        self._page_total = total
        self.page_label.setText(f"第 {page} / {total} 页")

    # --------------------------------------------------------------- status
    def _set_status(self, message: str) -> None:
        self.status_message.setText(message)

    def _set_busy_status(self, message: str) -> None:
        self._set_status(message)

    def _show_progress(self, visible: bool, current: int = 0, total: int = 1) -> None:
        if not visible:
            self.status_progress.hide()
            return
        self.status_progress.show()
        self.status_progress.setRange(0, max(1, total))
        self.status_progress.setValue(max(0, min(current, total)))
        self.status_progress.setFixedWidth(150)

    def _update_api_chip(self) -> None:
        if self.config.api_ready:
            text = f"API · {self.config.llm.model}"
            name = "Chip"
            tip = f"{self.config.llm.base_url} · {self.config.llm.model}"
        else:
            text = "API 未配置"
            name = "WarmChip"
            tip = "点击左下角齿轮配置 OpenAI 兼容接口"
        self.api_chip.setText(text if len(text) <= 28 else text[:28] + "…")
        self.api_chip.setObjectName(name)
        self.api_chip.setToolTip(tip)
        self.api_chip.style().unpolish(self.api_chip)
        self.api_chip.style().polish(self.api_chip)
        self.api_status.setText("API: 就绪" if self.config.api_ready else "API: 未配置")

    def _update_state_chip(self) -> None:
        texts = {
            "idle": "未开始",
            "ocr": "OCR 预处理",
            "running": "翻译中",
            "done": "已翻译",
            "partial": "部分完成",
            "error": "待处理",
        }
        text = texts.get(self._translation_state, "未开始")
        self.middle_state_chip.setText(text)
        name = "Chip" if self._translation_state in {"done"} else "WarmChip"
        self.middle_state_chip.setObjectName(name)
        self.middle_state_chip.style().unpolish(self.middle_state_chip)
        self.middle_state_chip.style().polish(self.middle_state_chip)

    def _update_actions(self) -> None:
        has_doc = self.document is not None
        busy_translate = self._translation_running or self._ocr_running or self._review_running
        has_translation = bool(self.translations)
        self.reparse_button.setEnabled(has_doc and not busy_translate)
        self.translate_button.setEnabled(has_doc and not busy_translate)
        self.review_button.setEnabled(
            has_doc
            and has_translation
            and not busy_translate
            and self.config.api_ready
        )
        self.review_button.setText("审查中…" if self._review_running else "审查")
        self.export_button.setEnabled(has_doc)
        self.import_project_button.setEnabled(not busy_translate)
        self.export_project_button.setEnabled(has_doc and not busy_translate and not self._chat_running)
        self.new_chat_button.setEnabled(has_doc and not self._chat_running)

    # ---------------------------------------------------------- cancellation
    def _cancel_active_tasks(self) -> None:
        self._cancel_prepare()
        self._cancel_translation()
        self._cancel_review()
        self._cancel_chat()
        if self._ocr_running:
            self._ocr_running = False
            self.ocr_client.cancel()

    def _cancel_prepare(self) -> None:
        worker = self._prepare_worker
        self._prepare_worker = None
        if worker and worker.isRunning():
            try:
                worker.progress.disconnect()
                worker.ready.disconnect()
                worker.failed.disconnect()
                worker.finished.disconnect()
            except Exception:
                pass
            worker.cancel()
            if not worker.wait(1200):
                self._retire(worker)
            else:
                worker.deleteLater()

    def _cancel_translation(self) -> None:
        worker = self._translation_worker
        self._translation_worker = None
        self._translation_running = False
        self._translation_dirty = False
        self._render_timer.stop()
        self.translation_progress.hide()
        if worker and worker.isRunning():
            try:
                worker.progress.disconnect()
                worker.partial.disconnect()
                worker.finished_ok.disconnect()
                worker.failed.disconnect()
                worker.finished.disconnect()
            except Exception:
                pass
            worker.cancel()
            if not worker.wait(1500):
                self._retire(worker)
            else:
                worker.deleteLater()

    def _cancel_review(self) -> None:
        worker = self._review_worker
        self._review_worker = None
        self._review_running = False
        self.review_button.setText("审查")
        if worker and worker.isRunning():
            try:
                worker.finished_ok.disconnect()
                worker.failed.disconnect()
                worker.finished.disconnect()
            except Exception:
                pass
            worker.requestInterruption()
            if not worker.wait(1200):
                self._retire(worker)
            else:
                worker.deleteLater()

    def _cancel_chat(self) -> None:
        worker = self._chat_worker
        self._chat_worker = None
        self._chat_running = False
        if worker and worker.isRunning():
            try:
                worker.delta.disconnect()
                worker.finished_ok.disconnect()
                worker.failed.disconnect()
                worker.finished.disconnect()
            except Exception:
                pass
            worker.cancel()
            if not worker.wait(1500):
                self._retire(worker)
            else:
                worker.deleteLater()

    def _retire(self, worker) -> None:
        self._retired.append(worker)

        def cleanup():
            try:
                if worker in self._retired:
                    self._retired.remove(worker)
            except Exception:
                pass
            worker.deleteLater()

        worker.finished.connect(cleanup)

    # -------------------------------------------------------------- dnd/close
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith(".pdf"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # type: ignore[override]
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() == ".pdf":
                self.load_pdf(path)
                event.acceptProposedAction()
                return
        event.ignore()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        self._closing = True
        self._render_timer.stop()
        self._cancel_prepare()
        self._cancel_translation()
        self._cancel_review()
        self._cancel_chat()
        try:
            self.ocr_client.shutdown()
        except Exception:
            pass
        self.pdf_view.shutdown()
        for worker in list(self._retired):
            try:
                if worker.isRunning():
                    if not worker.wait(2500):
                        # Last-resort exit path: never destroy a live QThread.
                        worker.terminate()
                        worker.wait(1000)
            except Exception:
                pass
        try:
            self.config.ui.window_width = self.width()
            self.config.ui.window_height = self.height()
            sizes = self.splitter.sizes()
            if len(sizes) == 3 and sum(sizes) > 0:
                self.config.ui.splitter_sizes = sizes
            self.config.save()
        except Exception:
            log.debug("could not save UI settings", exc_info=True)
        event.accept()
