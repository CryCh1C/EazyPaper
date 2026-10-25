from __future__ import annotations

import argparse
import ctypes
import os
import sys
import traceback
from pathlib import Path

# Environment must be pinned before importing PySide6/torch/transformers.
from .paths import APP_NAME, APP_VERSION, HOME, configure_process_environment

configure_process_environment()

from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from .config import AppConfig  # noqa: E402
from .logging_setup import get_logger, setup_logging  # noqa: E402
from .paths import resource_path  # noqa: E402
from .ui.main_window import MainWindow  # noqa: E402
from .ui.theme import apply_theme  # noqa: E402

log = get_logger("main")


def _set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("EasyPaper.PaperReader.1")
    except Exception:
        pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EasyPaper local PDF reader")
    parser.add_argument("pdf", nargs="?", help="optional PDF to open")
    parser.add_argument("--pdf", dest="pdf_option", help="optional PDF to open")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-pdf", default="", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-wait", type=int, default=6, help=argparse.SUPPRESS)
    parser.add_argument("--smoke-no-ocr", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def install_exception_hook(app: QApplication) -> None:
    def handle(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.error("Unhandled exception:\n%s", details)
        try:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("EasyPaper 发生错误")
            box.setText("程序遇到未处理的错误。详细信息已写入 logs/easypaper.log。")
            box.setDetailedText(details)
            box.exec()
        except Exception:
            pass

    sys.excepthook = handle


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.version:
        print(f"EasyPaper {APP_VERSION}")
        return 0
    setup_logging()
    _set_windows_app_id()
    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("EasyPaper")
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("EasyPaper")
    icon = resource_path("easypaper.ico")
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    apply_theme(app)
    install_exception_hook(app)
    log.info("EasyPaper %s starting; home=%s", APP_VERSION, HOME)

    try:
        config = AppConfig.load()
        apply_theme(app, config.ui.appearance, config.ui.color_theme)
        original_ocr = (config.ocr.enabled, config.ocr.mode)
        if args.smoke_no_ocr:
            config.ocr.enabled = False
            config.ocr.mode = "off"
        pdf = args.pdf_option or args.pdf
        initial = Path(pdf).expanduser() if pdf else None
        window = MainWindow(config, initial_pdf=initial)
        window.show()
        if args.smoke_test:
            smoke_pdf = Path(args.smoke_pdf).expanduser() if args.smoke_pdf else None

            def finish_smoke() -> None:
                import json

                from .paths import LOG_DIR

                payload = {
                    "ok": True,
                    "version": APP_VERSION,
                    "document_loaded": window.document is not None,
                    "pages": window.document.page_count if window.document else 0,
                    "blocks": len(window.document.blocks) if window.document else 0,
                    "page_widgets": len(window.pdf_view.pages),
                    "translation_state": window._translation_state,
                    "status": window.status_message.text(),
                    "python": sys.version,
                    "frozen": bool(getattr(sys, "frozen", False)),
                }
                output_dir = LOG_DIR
                output_dir.mkdir(parents=True, exist_ok=True)
                try:
                    window.grab().save(str(output_dir / "smoke.png"))
                    (output_dir / "smoke.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception:
                    log.exception("smoke result write failed")
                window.config.ocr.enabled, window.config.ocr.mode = original_ocr
                window.close()
                app.quit()

            if smoke_pdf and smoke_pdf.is_file():
                QTimer.singleShot(300, lambda: window.load_pdf(smoke_pdf))
            QTimer.singleShot(max(2, args.smoke_wait) * 1000, finish_smoke)
        return app.exec()
    except Exception:
        log.exception("fatal startup error")
        QMessageBox.critical(None, "EasyPaper 启动失败", traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
