"""Middle-pane HTML renderer backed by QtWebEngine.

QTextBrowser cannot render MathJax/KaTeX or CSS Grid.  This view loads a local
HTML file (never a remote URL), enables local file access for cached figure
images and KaTeX fonts, and handles Ctrl+wheel / Shift+wheel inside the page
via JavaScript.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from . import theme as theme_module


class _LocalPage(QWebEnginePage):
    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame) -> bool:  # type: ignore[override]
        from PySide6.QtWebEngineCore import QWebEnginePage as _Page

        if nav_type == _Page.NavigationType.NavigationTypeLinkClicked:
            return False
        return url.isLocalFile() or url.scheme() in {"about", "data", "qrc"}


class TranslationWebView(QWebEngineView):
    zoom_changed = Signal(int)
    load_failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._profile = QWebEngineProfile(self)  # off-the-record: no browser cache in user dir
        self._page = _LocalPage(self._profile, self)
        self.setPage(self._page)
        settings = self._page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled, False)
        self._page.loadFinished.connect(self._on_load_finished)
        self._page.titleChanged.connect(self._on_title_changed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._loaded = False
        self._zoom = 1.0
        self._pending_anchor = ""
        self._pending_reset = False
        self._sequence = 0
        self._last_file = ""
        self.apply_appearance()

    def apply_appearance(self) -> None:
        dark = theme_module.is_dark()
        panel = theme_module.COLORS.get("panel", "#FFFFFF")
        self._page.setBackgroundColor(QColor("#12161B" if dark else panel))
        self.setStyleSheet(
            f"QWebEngineView {{ background:{'#12161B' if dark else panel}; border:none; }}"
        )

    def set_html(self, html: str, base_dir: Path | str, keep_position: bool = True) -> None:
        base = Path(base_dir).resolve()
        base.mkdir(parents=True, exist_ok=True)
        self._sequence += 1
        sequence = self._sequence
        # Chromium may keep a handle on the currently loaded file.  Writing a
        # new file per update avoids Windows replace/sharing violations.
        target = base / f"translation-{sequence}.html"
        target.write_text(html, encoding="utf-8")
        if keep_position and self._loaded:
            self._page.runJavaScript(
                "window.__topBlock ? window.__topBlock() : ''",
                lambda anchor, seq=sequence, path=target: self._load_file(
                    path, str(anchor or ""), seq, reset=False
                ),
            )
        else:
            self._load_file(target, "", sequence, reset=True)

    def _load_file(self, target: Path, anchor: str, sequence: int, reset: bool = False) -> None:
        if sequence != self._sequence:
            return
        self._pending_anchor = anchor
        self._pending_reset = reset
        self._last_file = str(target)
        self._loaded = False
        self.load(QUrl.fromLocalFile(str(target)))

    def _on_load_finished(self, ok: bool) -> None:
        self._loaded = True
        if not ok:
            self.load_failed.emit("译文 HTML 加载失败")
            return
        anchor = self._pending_anchor
        reset = self._pending_reset
        self._pending_anchor = ""
        self._pending_reset = False
        script = (
            f"if (window.__setZoom) window.__setZoom({self._zoom:.4f});"
            f"if (window.__afterLoad) window.__afterLoad({json.dumps(anchor)});"
        )
        if reset:
            script += (
                "window.scrollTo(0,0);"
                "setTimeout(function(){window.scrollTo(0,0);},60);"
            )
        self._page.runJavaScript(script)
        self._cleanup_old_files()

    def _cleanup_old_files(self) -> None:
        try:
            base = Path(self._last_file).parent
            current = Path(self._last_file).name
            files = sorted(
                base.glob("translation-*.html"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for old in files:
                if old.name == current:
                    continue
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _on_title_changed(self, title: str) -> None:
        prefix = "EASYPAPER_ZOOM:"
        if title.startswith(prefix):
            try:
                percent = int(float(title[len(prefix):]))
            except ValueError:
                return
            self._zoom = max(0.5, min(2.5, percent / 100.0))
            self.zoom_changed.emit(percent)

    def zoom_by(self, step: int) -> None:
        if not self._loaded:
            return
        self._page.runJavaScript(f"window.__zoomBy ? window.__zoomBy({int(step)}) : 0")

    def reset_zoom(self) -> None:
        self._zoom = 1.0
        if self._loaded:
            self._page.runJavaScript("window.__setZoom ? window.__setZoom(1.0) : 0")

    def set_zoom(self, factor: float) -> None:
        self._zoom = max(0.5, min(2.5, factor))
        if self._loaded:
            self._page.runJavaScript(f"window.__setZoom ? window.__setZoom({self._zoom:.4f}) : 0")

    def reload_current(self) -> None:
        if self._last_file:
            self.load(QUrl.fromLocalFile(self._last_file))

    @property
    def zoom_percent(self) -> int:
        return int(round(self._zoom * 100))
