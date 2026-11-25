from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..config import REASONING_OPTIONS, AppConfig
from ..paths import CACHE_DIR, CONFIG_PATH, EXPORT_DIR, LOG_DIR
from ..workers import ConnectionTestWorker
from .theme import COLOR_OPTIONS, MODE_OPTIONS


class SettingsDialog(QDialog):
    saved = Signal(object)

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置 · EasyPaper")
        self.setMinimumWidth(720)
        self.setMinimumHeight(600)
        self.config = config.copy()
        self._test_worker: ConnectionTestWorker | None = None
        self._close_pending = False
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        tabs = QTabWidget()
        root.addWidget(tabs, 1)
        tabs.addTab(self._build_api_tab(), "AI 接口")
        tabs.addTab(self._build_ocr_tab(), "OCR 与翻译")
        tabs.addTab(self._build_appearance_tab(), "外观")
        tabs.addTab(self._build_about_tab(), "本地数据")

        footer = QHBoxLayout()
        self.footer_hint = QLabel("")
        self.footer_hint.setObjectName("Muted")
        footer.addWidget(self.footer_hint, 1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("保存")
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self._save)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.save_button)
        root.addLayout(footer)

    def _build_api_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://api.openai.com/v1")
        form.addRow("Base URL", self.base_url)

        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(7)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("sk-...")
        key_layout.addWidget(self.api_key, 1)
        self.show_key = QToolButton()
        self.show_key.setText("显示")
        self.show_key.setCheckable(True)
        self.show_key.toggled.connect(self._toggle_key)
        key_layout.addWidget(self.show_key)
        form.addRow("API Key", key_row)

        self.model = QLineEdit()
        self.model.setPlaceholderText("模型名称")
        form.addRow("模型", self.model)

        self.timeout = QSpinBox()
        self.timeout.setRange(10, 600)
        self.timeout.setSuffix(" 秒")
        form.addRow("请求超时", self.timeout)

        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.05)
        self.temperature.setDecimals(2)
        form.addRow("翻译温度", self.temperature)
        layout.addLayout(form)

        test_row = QHBoxLayout()
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self._test_connection)
        test_row.addWidget(self.test_button)
        self.test_status = QLabel()
        self.test_status.setWordWrap(True)
        self.test_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        test_row.addWidget(self.test_status, 1)
        layout.addLayout(test_row)
        layout.addStretch(1)
        return page

    def _build_ocr_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(10)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.ocr_enabled = QCheckBox("启用")
        form.addRow("OCR", self.ocr_enabled)
        self.ocr_mode = QComboBox()
        self.ocr_mode.addItem("自动（仅扫描页）", "auto")
        self.ocr_mode.addItem("全部页面", "always")
        self.ocr_mode.addItem("关闭", "off")
        form.addRow("识别范围", self.ocr_mode)

        model_row = QWidget()
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(6)
        self.model_dir = QLineEdit()
        model_layout.addWidget(self.model_dir, 1)
        browse_model = QPushButton("浏览…")
        browse_model.clicked.connect(lambda: self._browse_dir(self.model_dir))
        model_layout.addWidget(browse_model)
        form.addRow("模型目录", model_row)

        self.ocr_device = QComboBox()
        self.ocr_device.addItem("自动", "auto")
        self.ocr_device.addItem("CPU", "cpu")
        self.ocr_device.addItem("CUDA", "cuda")
        form.addRow("设备", self.ocr_device)

        self.ocr_dpi = QSpinBox()
        self.ocr_dpi.setRange(96, 300)
        self.ocr_dpi.setSuffix(" DPI")
        form.addRow("渲染 DPI", self.ocr_dpi)

        self.ocr_max_pixels = QSpinBox()
        self.ocr_max_pixels.setRange(200_000, 2_000_000)
        self.ocr_max_pixels.setSingleStep(100_000)
        form.addRow("OCR 像素上限", self.ocr_max_pixels)

        self.ocr_batch = QSpinBox()
        self.ocr_batch.setRange(1, 4)
        self.ocr_batch.setSuffix(" 页/批")
        self.ocr_batch.setToolTip("显存不足时逐页识别")
        form.addRow("OCR 批量", self.ocr_batch)
        self.ocr_prewarm = QCheckBox("启动时加载模型")
        form.addRow("预加载", self.ocr_prewarm)
        self.inline_latex = QCheckBox("识别为 LaTeX")
        form.addRow("行内公式", self.inline_latex)
        self.inline_limit = QSpinBox()
        self.inline_limit.setRange(0, 200)
        self.inline_limit.setSpecialValueText("全部")
        self.inline_limit.setSuffix(" 个/篇")
        form.addRow("公式上限", self.inline_limit)

        self.ocr_timeout = QSpinBox()
        self.ocr_timeout.setRange(60, 14400)
        self.ocr_timeout.setSuffix(" 秒")
        form.addRow("OCR 超时", self.ocr_timeout)

        self.target_language = QLineEdit()
        form.addRow("翻译目标语言", self.target_language)
        self.chunk_chars = QSpinBox()
        self.chunk_chars.setRange(800, 12000)
        self.chunk_chars.setSingleStep(200)
        self.chunk_chars.setSuffix(" 字符/批")
        form.addRow("翻译分块", self.chunk_chars)
        self.workers = QSpinBox()
        self.workers.setRange(1, 6)
        self.workers.setSuffix(" 并发")
        form.addRow("翻译并发", self.workers)
        self.skip_refs = QCheckBox("跳过翻译")
        form.addRow("参考文献", self.skip_refs)
        self.review_pass = QCheckBox("用 AI 检查页眉、页脚等冗余内容")
        form.addRow("完成后审查", self.review_pass)
        self.translation_thinking = QComboBox()
        for label, value in REASONING_OPTIONS:
            self.translation_thinking.addItem(label, value)
        self.translation_thinking.setToolTip(
            "自动设置仅适用于支持的推理模型。较高的强度可能增加耗时和费用。"
        )
        form.addRow("翻译思考强度", self.translation_thinking)
        layout.addLayout(form)

        self.ocr_status = QLabel("")
        self.ocr_status.setWordWrap(True)
        layout.addWidget(self.ocr_status)
        layout.addStretch(1)
        return page

    def _build_appearance_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(12)
        form = QFormLayout()
        form.setSpacing(10)
        self.appearance_mode = QComboBox()
        for label, value in MODE_OPTIONS:
            self.appearance_mode.addItem(label, value)
        form.addRow("明暗模式", self.appearance_mode)
        self.color_theme = QComboBox()
        for label, value in COLOR_OPTIONS:
            self.color_theme.addItem(label, value)
        form.addRow("颜色主题", self.color_theme)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    def _build_about_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(10)
        form = QFormLayout()
        for label, path in (("配置", CONFIG_PATH), ("缓存", CACHE_DIR), ("日志", LOG_DIR), ("导出", EXPORT_DIR)):
            value = QLabel(str(path))
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True)
            form.addRow(label, value)
        layout.addLayout(form)
        privacy = QLabel("翻译和问答会向所选 AI 接口发送所需文本。请妥善保管含明文密钥的配置文件。")
        privacy.setObjectName("Muted")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------- load
    def _load(self) -> None:
        c = self.config
        self.base_url.setText(c.llm.base_url)
        self.api_key.setText(c.llm.api_key)
        self.model.setText(c.llm.model)
        self.timeout.setValue(c.llm.timeout)
        self.temperature.setValue(c.llm.temperature)

        self.ocr_enabled.setChecked(c.ocr.enabled)
        index = self.ocr_mode.findData(c.ocr.mode)
        self.ocr_mode.setCurrentIndex(max(0, index))
        self.model_dir.setText(c.ocr.model_dir)
        index = self.ocr_device.findData(c.ocr.device)
        self.ocr_device.setCurrentIndex(max(0, index))
        self.ocr_dpi.setValue(c.ocr.dpi)
        self.ocr_max_pixels.setValue(c.ocr.max_pixels)
        self.ocr_batch.setValue(c.ocr.batch_size)
        self.ocr_prewarm.setChecked(c.ocr.prewarm)
        self.inline_latex.setChecked(c.ocr.inline_latex)
        self.inline_limit.setValue(c.ocr.inline_formula_limit)
        self.ocr_timeout.setValue(c.ocr.timeout_seconds)
        self.target_language.setText(c.translation.target_language)
        self.chunk_chars.setValue(c.translation.chunk_chars)
        self.workers.setValue(c.translation.max_workers)
        self.skip_refs.setChecked(c.translation.skip_references)
        self.review_pass.setChecked(c.translation.review_pass)
        index = self.translation_thinking.findData(c.translation.reasoning_effort)
        self.translation_thinking.setCurrentIndex(index if index >= 0 else 0)
        index = self.appearance_mode.findData(c.ui.appearance)
        self.appearance_mode.setCurrentIndex(index if index >= 0 else 0)
        index = self.color_theme.findData(c.ui.color_theme)
        self.color_theme.setCurrentIndex(index if index >= 0 else 0)
        self._update_ocr_status()
        self.model_dir.textChanged.connect(self._update_ocr_status)

    def _update_ocr_status(self) -> None:
        path = Path(self.model_dir.text().strip()).expanduser()
        if not path.is_absolute():
            path = CONFIG_PATH.parent / path
        model_ok = (path / "config.json").is_file() and (path / "model.safetensors").is_file()
        self.ocr_status.setText("OCR 模型就绪" if model_ok else "缺少 OCR 模型文件")
        self.ocr_status.setToolTip("需要 config.json 和 model.safetensors")
        self.ocr_status.setStyleSheet("color:#0F766E;" if model_ok else "color:#C2410C;")

    # ------------------------------------------------------------------ actions
    def _toggle_key(self, visible: bool) -> None:
        self.api_key.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self.show_key.setText("隐藏" if visible else "显示")

    def _browse_dir(self, target: QLineEdit) -> None:
        start = target.text().strip() or str(CONFIG_PATH.parent)
        directory = QFileDialog.getExistingDirectory(self, "选择 OXR 模型目录", start)
        if directory:
            target.setText(directory)

    def _browse_python(self, target: QLineEdit) -> None:
        start = target.text().strip() or str(CONFIG_PATH.parent)
        path, _ = QFileDialog.getOpenFileName(self, "选择 python.exe", start, "Python (python.exe);;所有文件 (*.*)")
        if path:
            target.setText(path)

    def _collect(self) -> AppConfig:
        config = self.config.copy()
        config.llm.base_url = self.base_url.text().strip()
        config.llm.api_key = self.api_key.text().strip()
        config.llm.model = self.model.text().strip()
        config.llm.timeout = self.timeout.value()
        config.llm.temperature = self.temperature.value()
        config.ocr.enabled = self.ocr_enabled.isChecked()
        config.ocr.mode = str(self.ocr_mode.currentData())
        config.ocr.model_dir = self.model_dir.text().strip()
        config.ocr.device = str(self.ocr_device.currentData())
        config.ocr.dpi = self.ocr_dpi.value()
        config.ocr.max_pixels = self.ocr_max_pixels.value()
        config.ocr.batch_size = self.ocr_batch.value()
        config.ocr.prewarm = self.ocr_prewarm.isChecked()
        config.ocr.inline_latex = self.inline_latex.isChecked()
        config.ocr.inline_formula_limit = self.inline_limit.value()
        config.ocr.timeout_seconds = self.ocr_timeout.value()
        config.translation.target_language = self.target_language.text().strip() or "简体中文"
        config.translation.chunk_chars = self.chunk_chars.value()
        config.translation.max_workers = self.workers.value()
        config.translation.skip_references = self.skip_refs.isChecked()
        config.translation.review_pass = self.review_pass.isChecked()
        config.translation.reasoning_effort = self.translation_thinking.currentData() or "auto"
        config.ui.appearance = self.appearance_mode.currentData() or "system"
        config.ui.color_theme = self.color_theme.currentData() or "teal"
        config.validate()
        return config

    def _test_connection(self) -> None:
        if self._test_worker is not None and self._test_worker.isRunning():
            return
        config = self._collect()
        if not config.llm.base_url or not config.llm.model:
            self._set_test_status("请先填写 Base URL 和模型名称。", ok=False)
            return
        if not config.llm.api_key:
            self._set_test_status("请先填写 API Key。", ok=False)
            return
        self.test_button.setEnabled(False)
        self.test_button.setText("测试中…")
        self.save_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self._set_test_status("连接中…", ok=None)
        self._test_worker = ConnectionTestWorker(config, self)
        self._test_worker.result_ready.connect(self._on_test_result)
        self._test_worker.finished.connect(self._on_test_finished)
        self._test_worker.start()

    def _on_test_result(self, result: dict) -> None:
        ok = bool(result.get("ok"))
        message = str(result.get("message", ""))
        latency = int(result.get("latency_ms", 0) or 0)
        models = result.get("models") or []
        if models:
            message += f"  可用模型 {len(models)} 个。"
        if not ok and result.get("detail"):
            message += f"\n{result['detail']}"
        self._set_test_status(message, ok=ok, latency=latency)

    def _on_test_finished(self) -> None:
        self.test_button.setEnabled(True)
        self.test_button.setText("测试连接")
        self.save_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        if self._test_worker is not None:
            self._test_worker.deleteLater()
            self._test_worker = None
        if self._close_pending:
            self.reject()

    def _set_test_status(self, message: str, ok: bool | None, latency: int = 0) -> None:
        prefix = "✓ " if ok else ("✗ " if ok is False else "… ")
        if latency:
            message = f"{message}"
        self.test_status.setText(prefix + message)
        if ok is True:
            color = "#0F766E"
        elif ok is False:
            color = "#C2410C"
        else:
            color = "#6F746F"
        self.test_status.setStyleSheet(f"color:{color}; font-size:9pt;")

    def _save(self) -> None:
        config = self._collect()
        try:
            config.save()
        except Exception as exc:
            self.footer_hint.setText(f"保存失败：{exc}")
            self.footer_hint.setStyleSheet("color:#C2410C;")
            return
        self.saved.emit(config)
        self.accept()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._test_worker is not None and self._test_worker.isRunning():
            self._close_pending = True
            self.footer_hint.setText("连接测试完成后关闭…")
            event.ignore()
            return
        super().closeEvent(event)
