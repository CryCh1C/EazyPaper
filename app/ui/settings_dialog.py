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
    QFrame,
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
from ..paths import CONFIG_PATH, CACHE_DIR, LOG_DIR
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

        title = QLabel("设置")
        title.setObjectName("AppTitle")
        root.addWidget(title)
        subtitle = QLabel(f"配置文件：{CONFIG_PATH} · 所有缓存位于 {CACHE_DIR} · 日志位于 {LOG_DIR}")
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        tabs = QTabWidget()
        root.addWidget(tabs, 1)
        tabs.addTab(self._build_api_tab(), "AI 接口")
        tabs.addTab(self._build_ocr_tab(), "OCR 与翻译")
        tabs.addTab(self._build_appearance_tab(), "外观")
        tabs.addTab(self._build_about_tab(), "关于 / 数据")

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

        note = QLabel(
            "支持 OpenAI、Azure OpenAI 兼容网关、DeepSeek、通义千问兼容模式、Ollama / LM Studio 等。"
            "base_url 通常以 /v1 结尾。密钥只保存在本机 config.json。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)

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
        self.model.setPlaceholderText("例如 gpt-4o-mini / deepseek-chat / qwen2.5:14b")
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
        self.test_status = QLabel("尚未测试")
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
        note = QLabel(
            "数字版 PDF 优先使用内嵌文字层（保真度最高）；扫描页才会调用本地 PaddleOCR-VL。"
            "OCR 在独立进程中运行，不会阻塞界面。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.ocr_enabled = QCheckBox("启用本地 OCR")
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

        python_row = QWidget()
        python_layout = QHBoxLayout(python_row)
        python_layout.setContentsMargins(0, 0, 0, 0)
        python_layout.setSpacing(6)
        self.python_path = QLineEdit()
        self.python_path.setPlaceholderText("留空自动使用 .venv\\Scripts\\python.exe")
        python_layout.addWidget(self.python_path, 1)
        browse_python = QPushButton("浏览…")
        browse_python.clicked.connect(lambda: self._browse_python(self.python_path))
        python_layout.addWidget(browse_python)
        form.addRow("OCR Python", python_row)

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
        self.ocr_batch.setToolTip("GPU 显存足够时批量识别可提高吞吐；显存不足会自动回退逐页。")
        form.addRow("OCR 批量", self.ocr_batch)
        self.ocr_prewarm = QCheckBox("启动后预加载 OCR 模型（隐藏首次模型加载等待）")
        form.addRow("预加载", self.ocr_prewarm)

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
        self.skip_refs = QCheckBox("跳过参考文献条目的翻译")
        form.addRow("参考文献", self.skip_refs)
        self.review_pass = QCheckBox("翻译完成后调用 AI 整体审查，去除页眉/页脚等冗余信息")
        form.addRow("完成后审查", self.review_pass)
        self.translation_thinking = QComboBox()
        for label, value in REASONING_OPTIONS:
            self.translation_thinking.addItem(label, value)
        self.translation_thinking.setToolTip(
            "自动：仅对识别到的推理模型发送 reasoning_effort；"
            "其他模型不发送，避免兼容性问题。强度越高越慢、越贵。"
        )
        form.addRow("翻译思考强度", self.translation_thinking)
        thinking_note = QLabel(
            "费用提示：翻译默认“自动”，普通模型不产生思考 token；"
            "使用 o 系列/GPT-5 时默认取“低”，也可设“关闭思考”。"
        )
        thinking_note.setObjectName("Muted")
        thinking_note.setWordWrap(True)
        form.addRow("", thinking_note)
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
        note = QLabel(
            "深色模式会同步调整界面、译文、表格和问答区。"
            "“跟随系统”会在 Windows 明暗模式变化时自动切换；保存设置后立即生效。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _build_about_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(10)
        text = QLabel(
            "<b>EasyPaper 1.0</b><br><br>"
            "轻量本地论文阅读 / 翻译 / 问答工具。<br>"
            "Python 3.14 · PySide6 · PyMuPDF · PaddleOCR-VL · OpenAI 兼容 API。<br><br>"
            "<b>数据目录</b><br>"
            f"配置：{CONFIG_PATH}<br>"
            f"结构化缓存：{CACHE_DIR}\\&lt;PDF 指纹&gt;\\structure.json<br>"
            f"翻译缓存：{CACHE_DIR}\\&lt;PDF 指纹&gt;\\translation-&lt;签名&gt;.json<br>"
            f"日志：{LOG_DIR}\\easypaper.log、ocr-worker.log<br>"
            f"导出：{CONFIG_PATH.parent}\\exports<br><br>"
            "<b>隐私</b><br>"
            "PDF 与 OCR 均在本机处理。只有翻译和问答所需的文本片段会发送到你配置的 AI 接口。"
            "API Key 以明文形式保存在本机 config.json，请勿分享该文件。"
        )
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(text)
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
        self.python_path.setText(c.ocr.python_path)
        index = self.ocr_device.findData(c.ocr.device)
        self.ocr_device.setCurrentIndex(max(0, index))
        self.ocr_dpi.setValue(c.ocr.dpi)
        self.ocr_max_pixels.setValue(c.ocr.max_pixels)
        self.ocr_batch.setValue(c.ocr.batch_size)
        self.ocr_prewarm.setChecked(c.ocr.prewarm)
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
        self.python_path.textChanged.connect(self._update_ocr_status)

    def _update_ocr_status(self) -> None:
        path = Path(self.model_dir.text().strip()).expanduser()
        if not path.is_absolute():
            path = CONFIG_PATH.parent / path
        model_ok = (path / "config.json").is_file() and (path / "model.safetensors").is_file()
        python = self.python_path.text().strip()
        python_path = Path(python).expanduser() if python else None
        if python_path is not None and not python_path.is_absolute():
            python_path = CONFIG_PATH.parent / python_path
        if python_path is not None and not python_path.is_file():
            python_ok = False
        else:
            python_ok = True
        pieces = []
        if model_ok:
            pieces.append("✓ 已找到 config.json / model.safetensors")
        else:
            pieces.append("✗ 模型目录不完整（需要 config.json 与 model.safetensors）")
        if not python_ok:
            pieces.append("✗ 指定的 OCR Python 不存在")
        elif not python:
            pieces.append("OCR Python 将自动从 .venv 或 ocr-runtime 查找")
        self.ocr_status.setText("  ·  ".join(pieces))
        self.ocr_status.setStyleSheet("color:#0F766E;" if model_ok and python_ok else "color:#C2410C;")

    # ------------------------------------------------------------------ actions
    def _toggle_key(self, visible: bool) -> None:
        self.api_key.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self.show_key.setText("隐藏" if visible else "显示")

    def _browse_dir(self, target: QLineEdit) -> None:
        start = target.text().strip() or str(CONFIG_PATH.parent)
        directory = QFileDialog.getExistingDirectory(self, "选择 PaddleOCR-VL 模型目录", start)
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
        config.ocr.python_path = self.python_path.text().strip()
        config.ocr.device = str(self.ocr_device.currentData())
        config.ocr.dpi = self.ocr_dpi.value()
        config.ocr.max_pixels = self.ocr_max_pixels.value()
        config.ocr.batch_size = self.ocr_batch.value()
        config.ocr.prewarm = self.ocr_prewarm.isChecked()
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
        self._set_test_status("正在连接接口并验证鉴权，请稍候…", ok=None)
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
            self.footer_hint.setText("连接测试仍在进行，完成后窗口会自动关闭…")
            event.ignore()
            return
        super().closeEvent(event)
