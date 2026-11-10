"""Look and feel: colour tokens, fonts and the app-wide stylesheet.

Buttons take a `kind` property: "primary" (accent), "ghost" (borderless)
or none (outlined). Frames with objectName "card" get a raised surface,
and labels with objectName "muted", "title" or "h2" get text styles."""

import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QApplication, QComboBox, QSlider, QStyleFactory,
                               QTabBar)

from ..core.util import app_data_dir

DARK = dict(
    bg="#111214", surface="#18191c", surface2="#1f2125", hover="#26282d", border="#2a2c31",
    border_strong="#3a3d44", text="#e9eaed", muted="#9a9fa9", faint="#6b7079",
    accent="#5b8cff", accent_hover="#7299ff", accent_press="#4874e0", on_accent="#ffffff",
    selection="rgba(91,140,255,0.20)", danger="#ff6b6b", video="#000000",
)
LIGHT = dict(
    bg="#f5f6f8", surface="#ffffff", surface2="#eef0f3", hover="#e6e9ee", border="#dde0e6",
    border_strong="#c5cad3", text="#1b1d21", muted="#5d6470", faint="#8b919c",
    accent="#3d6df2", accent_hover="#5580f5", accent_press="#2f5cd8", on_accent="#ffffff",
    selection="rgba(61,109,242,0.14)", danger="#d93f3f", video="#0b0c0e",
)

# Colours the custom-painted widgets (timeline, session view) read.
current = dict(DARK)


def _svg(name: str, colour: str, body: str) -> str:
    """Write a tiny tinted SVG for stylesheet use and return its url path."""
    for folder in (app_data_dir() / "theme", Path(tempfile.gettempdir()) / "Trimmer-theme"):
        path = folder / f"{name}-{colour.strip('#')}.svg"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(body.replace("COLOUR", colour), encoding="utf-8")
            return path.as_posix()
        except OSError:
            continue
    return ""


def _icons(t):
    check = _svg("check", t["on_accent"], '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" '
                 'viewBox="0 0 12 12"><path d="M2.5 6.2l2.3 2.3 4.7-5" fill="none" stroke="COLOUR" '
                 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    chevron = _svg("chevron", t["muted"], '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
                   'viewBox="0 0 10 10"><path d="M2 3.5l3 3 3-3" fill="none" stroke="COLOUR" '
                   'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    up = _svg("up", t["muted"], '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8" viewBox="0 0 8 8">'
              '<path d="M1.5 5l2.5-2.5 2.5 2.5" fill="none" stroke="COLOUR" stroke-width="1.4" '
              'stroke-linecap="round"/></svg>')
    down = _svg("down", t["muted"], '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8" viewBox="0 0 8 8">'
                '<path d="M1.5 3l2.5 2.5 2.5-2.5" fill="none" stroke="COLOUR" stroke-width="1.4" '
                'stroke-linecap="round"/></svg>')
    return check, chevron, up, down


def stylesheet(t) -> str:
    check, chevron, up, down = _icons(t)
    return f"""
* {{ outline: none; }}
QWidget {{ color: {t['text']}; }}
QMainWindow, QDialog {{ background: {t['bg']}; }}
QToolTip {{ background: {t['surface2']}; color: {t['text']}; border: 1px solid {t['border']};
            border-radius: 6px; padding: 5px 8px; }}

QLabel#title {{ font-size: 15pt; font-weight: 600; }}
QLabel#h2 {{ font-size: 10.5pt; font-weight: 600; }}
QLabel#muted {{ color: {t['muted']}; }}
QLabel#mono {{ font-family: "Cascadia Mono", "Consolas", "JetBrains Mono", monospace; color: {t['muted']}; }}
QLabel#brand {{ font-size: 12.5pt; font-weight: 700; padding: 0 14px 0 4px; }}

QFrame#card, QGroupBox {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QGroupBox {{ margin-top: 0px; padding: 34px 14px 12px 14px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: padding; subcontrol-position: top left; left: 14px; top: 10px;
                    padding: 0; }}
QGroupBox::indicator {{ width: 16px; height: 16px; margin-right: 6px; }}
QFrame#card QLabel, QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QRadioButton {{ background: transparent;
                    font-weight: normal; }}

QFrame#video {{ background: {t['video']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QFrame#card QListView {{ background: transparent; border: none; padding: 0; }}

/* Buttons */
QPushButton, QToolButton {{ background: {t['surface2']}; border: 1px solid {t['border']};
             border-radius: 7px; padding: 6px 14px; min-height: 18px; }}
QPushButton:hover, QToolButton:hover {{ background: {t['hover']}; border-color: {t['border_strong']}; }}
QPushButton:pressed {{ background: {t['border']}; }}
QPushButton:disabled {{ color: {t['faint']}; background: {t['surface']}; border-color: {t['border']}; }}
QPushButton[kind="primary"] {{ background: {t['accent']}; border: 1px solid {t['accent']};
             color: {t['on_accent']}; font-weight: 600; padding: 7px 18px; }}
QPushButton[kind="primary"]:hover {{ background: {t['accent_hover']}; border-color: {t['accent_hover']}; }}
QPushButton[kind="primary"]:pressed {{ background: {t['accent_press']}; }}
QPushButton[kind="primary"]:disabled {{ background: {t['surface2']}; border-color: {t['border']};
             color: {t['faint']}; }}
QPushButton[kind="ghost"] {{ background: transparent; border: 1px solid transparent; color: {t['muted']}; }}
QPushButton[kind="ghost"]:hover {{ background: {t['hover']}; color: {t['text']}; }}
QPushButton[kind="ghost"]:disabled {{ background: transparent; color: {t['faint']}; }}
QPushButton[kind="icon"] {{ background: transparent; border: 1px solid transparent; padding: 4px 9px;
             font-size: 11pt; min-width: 18px; }}
QPushButton[kind="icon"]:hover {{ background: {t['hover']}; }}

/* Toolbar */
QToolBar {{ background: {t['bg']}; border: none; border-bottom: 1px solid {t['border']};
            padding: 6px 10px; spacing: 2px; }}
QToolBar QToolButton {{ background: transparent; border: 1px solid transparent; color: {t['muted']};
            padding: 6px 12px; }}
QToolBar QToolButton:hover {{ background: {t['hover']}; color: {t['text']}; }}
QToolBar::separator {{ width: 1px; background: {t['border']}; margin: 6px 8px; }}

/* Tabs: underlined, no boxes */
QTabWidget::pane {{ border: none; background: {t['bg']}; }}
QTabWidget::tab-bar {{ left: 16px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {t['muted']}; padding: 10px 4px 9px 4px; margin: 0 12px 0 0;
                border: none; border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:hover {{ color: {t['text']}; }}
QTabBar::tab:selected {{ color: {t['text']}; border-bottom: 2px solid {t['accent']}; }}

/* Inputs */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {t['surface2']}; border: 1px solid {t['border']};
            border-radius: 7px; padding: 5px 9px; min-height: 18px;
            selection-background-color: {t['accent']}; }}
QToolButton#clear {{ background: transparent; border: none; border-radius: 11px; padding: 0; }}
QToolButton#clear:hover {{ background: {t['hover']}; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: {t['border_strong']}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {t['accent']}; }}
QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ color: {t['faint']}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({chevron}); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 8px;
            padding: 4px; selection-background-color: {t['selection']}; selection-color: {t['text']}; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {{
            border: none; background: transparent; width: 18px; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({up}); width: 8px; height: 8px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({down}); width: 8px; height: 8px; }}

QCheckBox, QRadioButton {{ spacing: 9px; padding: 3px 0; background: transparent; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {t['faint']}; }}
QCheckBox::indicator, QGroupBox::indicator {{ width: 16px; height: 16px; border-radius: 5px;
            border: 1px solid {t['border_strong']}; background: {t['surface2']}; }}
QCheckBox::indicator:hover, QGroupBox::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked, QGroupBox::indicator:checked {{ background: {t['accent']};
            border-color: {t['accent']}; image: url({check}); }}
QCheckBox::indicator:disabled {{ background: {t['surface']}; border-color: {t['border']}; }}
QCheckBox::indicator:checked:disabled {{ background: {t['border_strong']}; border-color: {t['border_strong']}; }}
QRadioButton::indicator {{ width: 16px; height: 16px; border-radius: 9px; border: 1px solid {t['border_strong']};
            background: {t['surface2']}; }}
QRadioButton::indicator:hover {{ border-color: {t['accent']}; }}
QRadioButton::indicator:checked {{ border-color: {t['accent']};
            background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 {t['on_accent']},
            stop:0.32 {t['on_accent']}, stop:0.40 {t['accent']}, stop:1 {t['accent']}); }}

/* Tables, lists, trees */
QTableView, QTreeView, QListView, QPlainTextEdit, QTextBrowser {{ background: {t['surface']};
            border: 1px solid {t['border']}; border-radius: 10px; gridline-color: transparent;
            selection-background-color: {t['selection']}; selection-color: {t['text']};
            alternate-background-color: {t['surface']}; }}
/* A thin divider between columns, in the header and down the list. */
QTableView::item {{ padding: 0 8px; border: none; border-right: 1px solid {t['border']}; }}
QTreeView::item {{ padding: 6px 4px; border: none; border-right: 1px solid {t['border']}; }}
QTreeView::indicator, QTableView::indicator {{ width: 15px; height: 15px; border-radius: 5px;
            border: 1px solid {t['border_strong']}; background: {t['surface2']}; }}
QTreeView::indicator:checked, QTableView::indicator:checked {{ background: {t['accent']};
            border-color: {t['accent']}; image: url({check}); }}
QListView::item {{ padding: 4px 8px; border-radius: 6px; }}
QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {{
            background: {t['selection']}; color: {t['text']}; }}
QTreeView::item:hover, QTableView::item:hover, QListView::item:hover {{ background: {t['hover']}; }}
QHeaderView {{ background: transparent; border: none; }}
/* Transparent, not surface: the list's rounded background shows through, so the header's square
   corners don't poke out past the rounded edge. */
QHeaderView::section {{ background: transparent; color: {t['muted']}; border: none;
            border-bottom: 1px solid {t['border']}; border-right: 1px solid {t['border']};
            padding: 8px 8px; font-weight: 600; }}
QHeaderView::section:last {{ border-right: none; }}
QTableCornerButton::section {{ background: {t['surface']}; border: none; }}
QPlainTextEdit {{ padding: 8px; font-family: "Cascadia Mono", "Consolas", "JetBrains Mono", monospace;
            color: {t['muted']}; }}

QProgressBar {{ background: {t['surface2']}; border: none; border-radius: 4px; height: 8px;
            text-align: center; color: {t['text']}; font-size: 8.5pt; margin: 9px 6px; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 4px; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QAbstractScrollArea::corner {{ background: transparent; border: none; }}   /* between two scroll bars */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {t['border_strong']}; border-radius: 3px; min-height: 30px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: {t['faint']}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
            background: none; border: none; width: 0; height: 0; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 12px; }}
QSplitter::handle:vertical {{ height: 12px; }}

QStatusBar {{ background: {t['bg']}; color: {t['muted']}; border-top: 1px solid {t['border']}; }}
QStatusBar QLabel {{ color: {t['muted']}; padding: 0 8px; }}
QMenu {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 5px; }}
QMenu::item:selected {{ background: {t['selection']}; }}
QMessageBox {{ background: {t['bg']}; }}
QDialogButtonBox QPushButton {{ min-width: 72px; }}
"""


def _fonts(app: QApplication):
    f = QFont()
    if sys.platform == "win32":
        f.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
    else:
        f.setFamilies(["Inter", "SF Pro Text", "Noto Sans", "DejaVu Sans"])
    f.setPointSizeF(9.75)
    f.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(f)


def system_scheme() -> str:
    """"light" or "dark", from the OS setting (dark when it can't be read)."""
    try:
        from PySide6.QtCore import Qt
        scheme = QApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Light:
            return "light"
        if scheme == Qt.ColorScheme.Dark:
            return "dark"
    except (AttributeError, RuntimeError):
        pass
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
        except OSError:
            pass
    return "dark"


_follow = {"on": False, "connected": False}


def _system_changed(*_):
    app = QApplication.instance()
    if _follow["on"] and app is not None:
        _apply(app, system_scheme())
        for w in app.allWidgets():
            w.update()


def apply_theme(app: QApplication, theme: str = "dark"):
    """theme is "dark", "light" or "system" (follows the OS, live)."""
    _follow["on"] = theme == "system"
    if _follow["on"] and not _follow["connected"]:
        try:
            app.styleHints().colorSchemeChanged.connect(_system_changed)
            _follow["connected"] = True
        except AttributeError:      # Qt older than 6.5: picked up at the next start
            pass
    _apply(app, system_scheme() if theme == "system" else theme)


class _SpinPadding(QObject):
    """Spin boxes ignore the stylesheet's padding (their text sat against the left edge), so give
    each one's text the same inset as the combo boxes as it's first shown."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Polish and isinstance(obj, QAbstractSpinBox):
            obj.lineEdit().setTextMargins(8, 0, 0, 0)
        return False


_spin_padding = _SpinPadding()


class _HandCursor(QObject):
    """A pointing hand over everything you can press (buttons, check boxes, drop-downs, tabs,
    sliders), so it's clear they do something. Disabled ones keep the arrow, and widgets that
    choose their own cursor keep it."""
    KINDS = (QAbstractButton, QComboBox, QTabBar, QSlider)

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind in (QEvent.Polish, QEvent.EnabledChange) and isinstance(obj, self.KINDS):
            ours = obj.property("_hand_cursor")
            if obj.testAttribute(Qt.WA_SetCursor) and not ours:
                return False              # a cursor of its own (e.g. the arrow on a line edit's clear button)
            if obj.isEnabled():
                obj.setCursor(Qt.PointingHandCursor)
                obj.setProperty("_hand_cursor", True)
            elif ours:
                obj.unsetCursor()
                obj.setProperty("_hand_cursor", False)
        return False


_hand_cursor = _HandCursor()


def _apply(app: QApplication, theme: str):
    t = LIGHT if theme == "light" else DARK
    current.clear()
    current.update(t)
    app.setStyle(QStyleFactory.create("Fusion"))
    _fonts(app)
    p = QPalette()
    for role, key in ((QPalette.Window, "bg"), (QPalette.WindowText, "text"), (QPalette.Base, "surface"),
                      (QPalette.AlternateBase, "surface2"), (QPalette.ToolTipBase, "surface2"),
                      (QPalette.ToolTipText, "text"), (QPalette.Text, "text"), (QPalette.Button, "surface2"),
                      (QPalette.ButtonText, "text"), (QPalette.Highlight, "accent"),
                      (QPalette.HighlightedText, "on_accent"), (QPalette.Link, "accent"),
                      (QPalette.PlaceholderText, "faint"), (QPalette.Mid, "border"), (QPalette.Dark, "border")):
        p.setColor(role, QColor(t[key]))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor(t["faint"]))
    app.setPalette(p)
    app.setStyleSheet(stylesheet(t))
    app.removeEventFilter(_spin_padding)
    app.installEventFilter(_spin_padding)
    app.removeEventFilter(_hand_cursor)
    app.installEventFilter(_hand_cursor)


def clear_icon_path() -> str:
    return _svg("clear", current["muted"], '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
                'viewBox="0 0 16 16"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" fill="none" stroke="COLOUR" '
                'stroke-width="1.6" stroke-linecap="round"/></svg>')


def colour(key: str) -> QColor:
    return QColor(current[key])


def set_kind(button, kind: str):
    """Mark a button primary / ghost / icon for the stylesheet."""
    button.setProperty("kind", kind)
    return button
