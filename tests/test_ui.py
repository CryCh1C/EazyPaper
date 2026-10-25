from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from app.config import AppConfig
from app.ui.main_window import MainWindow


def _shift_wheel(widget, delta=120):
    event = QWheelEvent(
        QPointF(80, 80),
        QPointF(80, 80),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ShiftModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


def _ctrl_wheel(widget, delta=120):
    event = QWheelEvent(
        QPointF(80, 80),
        QPointF(80, 80),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


def test_chat_thinking_combo_updates_config(qtbot, config):
    from app.ui.main_window import MainWindow

    config.ui.window_width = 1500
    config.ui.window_height = 950
    window = MainWindow(config)
    qtbot.addWidget(window)
    index = window.chat_thinking.findData("high")
    window.chat_thinking.setCurrentIndex(index)
    assert window.config.chat.reasoning_effort == "high"


def test_theme_dark_and_accent(qtbot):
    from app.ui import theme

    assert theme.apply_theme(QApplication.instance(), "dark", "blue") is True
    assert theme.is_dark() is True
    assert theme.COLORS["accent"] == "#60A5FA"
    assert theme.apply_theme(QApplication.instance(), "light", "red") is False
    assert theme.COLORS["accent"] == "#DC2626"


def test_project_buttons_and_markdown_export(qtbot, config):
    config.ui.window_width = 1500
    config.ui.window_height = 950
    window = MainWindow(config)
    qtbot.addWidget(window)
    assert window.import_project_button.text() == "导入项目"
    assert window.export_project_button.text() == "导出项目"
    assert window.export_button.text() == "导出"


def test_main_window_loads_pdf(qtbot, sample_pdf, config, monkeypatch):
    import app.pdf.cache as cache_module

    monkeypatch.setattr(cache_module, "CACHE_DIR", sample_pdf.parent / "cache-ui")
    window = MainWindow(config)
    qtbot.addWidget(window)
    window.show()
    window.load_pdf(sample_pdf)
    qtbot.waitUntil(lambda: window.document is not None, timeout=15000)
    qtbot.waitUntil(lambda: len(window.pdf_view.pages) == 3, timeout=5000)
    assert window.page_label.text().startswith("第 1 / 3 页")
    assert "EasyPaper" in window.translation_view.toPlainText()
    assert window._translation_state == "error"
    assert window.api_chip.text()
    assert window.left_zoom_label.text() == "100%"
    page = window.pdf_view.pages[0]
    assert page.words
    page._anchor = 0
    page._current = min(3, len(page.words) - 1)
    assert page.selection_text()
    assert "# " in window._document_markdown()

    window.pdf_view.zoom_by(1)
    qtbot.wait(100)
    assert window.left_zoom_label.text() != "100%"

    pdf_bar = window.pdf_view.scroll.horizontalScrollBar()
    pdf_bar.setRange(0, 1000)
    pdf_bar.setValue(500)
    _shift_wheel(window.pdf_view.scroll.viewport())
    assert pdf_bar.value() == 380

    mid_bar = window.translation_view.horizontalScrollBar()
    mid_bar.setRange(0, 1000)
    mid_bar.setValue(500)
    _shift_wheel(window.translation_view.viewport())
    assert mid_bar.value() == 380

    before_html = window.translation_view.toHtml()
    assert "<img" in before_html.lower()
    window.translation_view.scrollToAnchor("block-p2_b1")
    QApplication.processEvents()
    top_before = window.translation_view.top_anchor()
    assert top_before
    _ctrl_wheel(window.translation_view.viewport())
    qtbot.wait(100)
    assert window.middle_zoom_label.text() == "115%"
    after_html = window.translation_view.toHtml()
    assert after_html != before_html
    assert window.translation_view.top_anchor() == top_before
    assert window.review_button.text() in {"审查", "审查中…"}

    window.chat.attach_quote("selected source text", 1)
    assert window.chat.quote_text == "selected source text"
    assert window.chat.quote_page == 1
    assert window.chat.quote_frame.isVisible()
    window.chat.clear_quote()

    window.reset_zoom()
    assert window.left_zoom_label.text() == "100%"
    window.close()
    qtbot.wait(400)


def test_settings_dialog_fields(qtbot, config):
    from app.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)
    assert dialog.base_url.text() == config.llm.base_url
    assert dialog.model.text() == config.llm.model
    assert dialog.ocr_mode.currentData() == "off" or config.ocr.mode == "off"
    dialog.appearance_mode.setCurrentIndex(dialog.appearance_mode.findData("dark"))
    dialog.color_theme.setCurrentIndex(dialog.color_theme.findData("blue"))
    collected = dialog._collect()
    assert collected.ui.appearance == "dark"
    assert collected.ui.color_theme == "blue"
    dialog.close()
