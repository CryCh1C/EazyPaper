from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

MODE_OPTIONS = [
    ("跟随系统", "system"),
    ("浅色", "light"),
    ("深色", "dark"),
]

COLOR_OPTIONS = [
    ("青绿", "teal"),
    ("蓝色", "blue"),
    ("红色", "red"),
    ("紫色", "violet"),
    ("琥珀", "amber"),
]

LIGHT_BASE = {
    "bg": "#F3F0E9",
    "panel": "#FFFFFF",
    "panel_alt": "#FAF8F4",
    "border": "#DED8CC",
    "border_strong": "#C8C0B2",
    "ink": "#20262E",
    "muted": "#6F746F",
    "faint": "#989C96",
    "selection": "#BFE3DD",
    "pdf_bg": "#E7E3DA",
    "hover": "#F1EDE5",
    "scrollbar": "#C9C3B7",
    "scrollbar_hover": "#AFA89B",
}

DARK_BASE = {
    "bg": "#12161B",
    "panel": "#1B2027",
    "panel_alt": "#222831",
    "border": "#303841",
    "border_strong": "#46515D",
    "ink": "#E8EBEF",
    "muted": "#A3ACB6",
    "faint": "#7B8590",
    "selection": "#294A63",
    "pdf_bg": "#0E1114",
    "hover": "#2A313A",
    "scrollbar": "#4A555F",
    "scrollbar_hover": "#61707D",
}

# light accent, light accent dark, light soft, dark accent, dark soft, dark text
ACCENTS = {
    "teal": ("#0F766E", "#0B5E58", "#E4F2EF", "#14B8A6", "#183C39", "#5EEAD4"),
    "blue": ("#2563EB", "#1D4ED8", "#E6EEFF", "#60A5FA", "#1C3153", "#93C5FD"),
    "red": ("#DC2626", "#B91C1C", "#FDE8E8", "#F87171", "#472126", "#FCA5A5"),
    "violet": ("#7C3AED", "#6D28D9", "#F0E9FF", "#A78BFA", "#342657", "#C4B5FD"),
    "amber": ("#B45309", "#92400E", "#FDF0DC", "#FBBF24", "#46351B", "#FCD34D"),
}

IS_DARK = False
CURRENT_MODE = "system"
CURRENT_COLOR = "teal"
COLORS: dict[str, str] = {}


def _system_is_dark() -> bool:
    try:
        scheme = QApplication.styleHints().colorScheme()
        if getattr(Qt, "ColorScheme", None) is not None:
            if scheme == Qt.ColorScheme.Dark:
                return True
            if scheme == Qt.ColorScheme.Light:
                return False
    except Exception:
        pass
    try:
        window = QApplication.palette().color(QPalette.ColorRole.Window)
        return window.lightness() < 128
    except Exception:
        return False


def resolve_dark(mode: str) -> bool:
    value = (mode or "system").lower()
    if value == "dark":
        return True
    if value == "light":
        return False
    return _system_is_dark()


def is_dark() -> bool:
    return IS_DARK


def color(key: str, default: str = "") -> str:
    return COLORS.get(key, default)


def apply_theme(app: QApplication, mode: str = "system", color_theme: str = "teal") -> bool:
    """Apply light/dark mode and the selected accent color."""

    global IS_DARK, CURRENT_MODE, CURRENT_COLOR, COLORS
    if color_theme not in ACCENTS:
        color_theme = "teal"
    CURRENT_MODE = mode if mode in {"system", "light", "dark"} else "system"
    CURRENT_COLOR = color_theme
    IS_DARK = resolve_dark(CURRENT_MODE)

    base = dict(DARK_BASE if IS_DARK else LIGHT_BASE)
    light_accent, light_dark, light_soft, dark_accent, dark_soft, dark_text = ACCENTS[color_theme]
    if IS_DARK:
        base["accent"] = dark_accent
        base["accent_dark"] = light_accent
        base["accent_soft"] = dark_soft
        base["accent_text"] = dark_text
        base["warm"] = "#FB923C"
        base["warm_soft"] = "#402A1C"
    else:
        base["accent"] = light_accent
        base["accent_dark"] = light_dark
        base["accent_soft"] = light_soft
        base["accent_text"] = light_accent
        base["warm"] = "#C2410C"
        base["warm_soft"] = "#FCEFE7"
    COLORS = base

    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS["bg"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["ink"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLORS["panel"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(COLORS["panel_alt"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLORS["ink"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLORS["panel"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLORS["ink"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLORS["accent"]))
    palette.setColor(
        QPalette.ColorRole.HighlightedText,
        QColor("#FFFFFF" if not IS_DARK else "#08110F"),
    )
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(COLORS["ink"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(COLORS["panel"]))
    app.setPalette(palette)
    app.setStyleSheet(_qss())
    app.setProperty("easypaperDark", IS_DARK)
    return IS_DARK


def _qss() -> str:
    c = COLORS
    return f"""
    QMainWindow, QDialog {{
        background: {c['bg']};
    }}
    QWidget {{
        color: {c['ink']};
    }}
    QLabel#AppTitle {{
        font-size: 16pt;
        font-weight: 700;
        color: {c['ink']};
    }}
    QLabel#AppSubtitle {{
        color: {c['muted']};
        font-size: 9pt;
    }}
    QLabel#PanelTitle {{
        font-size: 11pt;
        font-weight: 700;
        color: {c['ink']};
    }}
    QLabel#PanelHint, QLabel#Muted {{
        color: {c['muted']};
        font-size: 9pt;
    }}
    QLabel#Chip {{
        background: {c['accent_soft']};
        color: {c['accent_text']};
        border: 1px solid {c['border']};
        border-radius: 9px;
        padding: 2px 8px;
        font-size: 8.5pt;
    }}
    QLabel#WarmChip {{
        background: {c['warm_soft']};
        color: {c['warm']};
        border: 1px solid {c['border']};
        border-radius: 9px;
        padding: 2px 8px;
        font-size: 8.5pt;
    }}
    QFrame#Panel {{
        background: {c['panel']};
        border: 1px solid {c['border']};
        border-radius: 12px;
    }}
    QFrame#HeaderBar {{
        background: {c['panel']};
        border-bottom: 1px solid {c['border']};
    }}
    QFrame#PanelHeader {{
        background: {c['panel']};
        border: none;
        border-bottom: 1px solid {c['border']};
        border-top-left-radius: 12px;
        border-top-right-radius: 12px;
    }}
    QFrame#FooterBar {{
        background: {c['panel_alt']};
        border: none;
        border-top: 1px solid {c['border']};
        border-bottom-left-radius: 12px;
        border-bottom-right-radius: 12px;
    }}
    QSplitter::handle {{
        background: transparent;
        width: 8px;
    }}
    QSplitter::handle:hover {{
        background: {c['border_strong']};
        border-radius: 4px;
    }}
    QPushButton, QToolButton {{
        background: {c['panel']};
        border: 1px solid {c['border_strong']};
        border-radius: 8px;
        padding: 5px 11px;
        color: {c['ink']};
    }}
    QPushButton:hover, QToolButton:hover {{
        background: {c['hover']};
        border-color: {c['accent']};
    }}
    QPushButton:pressed, QToolButton:pressed {{
        background: {c['accent_soft']};
    }}
    QPushButton:disabled, QToolButton:disabled {{
        color: {c['faint']};
        border-color: {c['border']};
        background: {c['panel_alt']};
    }}
    QPushButton#Primary {{
        background: {c['accent']};
        color: {c['panel']};
        border: 1px solid {c['accent']};
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{
        background: {c['accent_dark']};
    }}
    QPushButton#Primary:disabled {{
        background: {c['border_strong']};
        border-color: {c['border_strong']};
        color: {c['faint']};
    }}
    QPushButton#Warm {{
        color: {c['warm']};
        border-color: {c['border']};
        background: {c['warm_soft']};
    }}
    QToolButton#GearButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 8px;
        padding: 6px 9px;
        font-size: 13pt;
        color: {c['muted']};
    }}
    QToolButton#GearButton:hover {{
        background: {c['panel_alt']};
        border-color: {c['border']};
        color: {c['accent_text']};
    }}
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background: {c['panel']};
        border: 1px solid {c['border_strong']};
        border-radius: 8px;
        padding: 5px 8px;
        selection-background-color: {c['selection']};
        selection-color: {c['ink']};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
    QDoubleSpinBox:focus, QComboBox:focus {{
        border: 1px solid {c['accent']};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QComboBox QAbstractItemView {{
        background: {c['panel']};
        color: {c['ink']};
        selection-background-color: {c['accent_soft']};
    }}
    QScrollArea {{
        border: none;
        background: transparent;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {c['scrollbar']};
        min-height: 30px;
        border-radius: 4px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {c['scrollbar_hover']};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: none;
        border: none;
        height: 0;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 10px;
        margin: 2px;
    }}
    QScrollBar::handle:horizontal {{
        background: {c['scrollbar']};
        min-width: 30px;
        border-radius: 4px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {c['scrollbar_hover']};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
        background: none;
        border: none;
        width: 0;
    }}
    QStatusBar {{
        background: {c['panel']};
        border-top: 1px solid {c['border']};
        color: {c['muted']};
        font-size: 8.5pt;
    }}
    QStatusBar::item {{
        border: none;
    }}
    QProgressBar {{
        background: {c['panel_alt']};
        border: none;
        border-radius: 3px;
        height: 6px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background: {c['accent']};
        border-radius: 3px;
    }}
    QMenu {{
        background: {c['panel']};
        color: {c['ink']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        padding: 4px;
    }}
    QMenu::item {{
        padding: 6px 22px 6px 12px;
        border-radius: 5px;
    }}
    QMenu::item:selected {{
        background: {c['accent_soft']};
    }}
    QToolTip {{
        background: {c['ink']};
        color: {c['panel']};
        border: none;
        padding: 5px 8px;
    }}
    QCheckBox {{
        spacing: 7px;
    }}
    QCheckBox::indicator {{
        width: 15px;
        height: 15px;
        border: 1px solid {c['border_strong']};
        border-radius: 4px;
        background: {c['panel']};
    }}
    QCheckBox::indicator:checked {{
        background: {c['accent']};
        border-color: {c['accent']};
    }}
    QTabWidget::pane {{
        border: 1px solid {c['border']};
        border-radius: 8px;
        background: {c['panel']};
    }}
    QTabBar::tab {{
        background: transparent;
        padding: 7px 16px;
        color: {c['muted']};
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:selected {{
        color: {c['accent_text']};
        border-bottom: 2px solid {c['accent']};
        font-weight: 600;
    }}
    """
