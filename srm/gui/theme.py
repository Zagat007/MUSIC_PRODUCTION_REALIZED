"""Shared visual theme: deep graphite surfaces, ivory text, one gold accent and a distinct colour for
every stage of the workflow, so the next action is always the brightest thing on the screen."""

from __future__ import annotations

BG, PANEL, CARD, CARD_HI, LINE = "#0c0d11", "#12141a", "#181b23", "#20242e", "#2b303c"
TEXT, MUTED, FAINT = "#f4f0e6", "#9da3b3", "#6b7182"
GOLD, GOLD_HI, GOLD_LO = "#d8b56c", "#f0d596", "#a8833c"
GOOD, WARN, BAD = "#4fd69c", "#f2b84b", "#ff6b6b"

# One colour per stage (button, progress, table marks)
STAGE_COLORS = {
    "analyze": "#5ab0f2",   # sapphire
    "versions": "#a98bf5",  # amethyst
    "repair": "#3fd0a0",    # emerald
    "master": "#f5a13a",    # amber
    "listen": "#f27aa9",    # rose
    "release": GOLD,        # gold
}


def _shade(hex_color: str, factor: float) -> str:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(c * factor))) for c in (r, g, b)))


def stage_button_css(color: str) -> str:
    """Filled button in a stage colour (used through setStyleSheet on the button itself)."""
    return (f"QPushButton {{ background:{color}; color:#0c0d11; border:0; border-radius:9px; padding:9px 14px; "
            f"font-weight:700; }} QPushButton:hover {{ background:{_shade(color, 1.15)}; }} "
            f"QPushButton:pressed {{ background:{_shade(color, 0.85)}; }} "
            f"QPushButton:disabled {{ background:{CARD_HI}; color:{FAINT}; }}")


LUX_STYLE = f"""
QWidget {{ background:{BG}; color:{TEXT}; font-family:'Segoe UI Variable Text','Segoe UI'; font-size:10pt; }}
QLabel {{ background:transparent; }}
QToolTip {{ background:{CARD_HI}; color:{TEXT}; border:1px solid {GOLD_LO}; padding:8px; border-radius:6px; }}

QFrame#TopBar {{ background:{PANEL}; border-bottom:1px solid {LINE}; }}
QFrame#Sidebar {{ background:{PANEL}; border-right:1px solid {LINE}; }}
QFrame#Card {{ background:{CARD}; border:1px solid {LINE}; border-radius:14px; }}
QFrame#Hero {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #1d2029, stop:1 #14161c);
               border:1px solid {LINE}; border-radius:16px; }}
QFrame#StageCard {{ background:{CARD}; border:1px solid {LINE}; border-radius:14px; }}
QFrame#StageCard[done="true"] {{ border:1px solid #3a4a44; background:#161d1c; }}

QLabel#Brand {{ font-size:13pt; font-weight:700; letter-spacing:3px; color:{GOLD}; }}
QLabel#Title {{ font-size:25pt; font-weight:700; color:{TEXT}; }}
QLabel#AlbumTitle {{ font-size:21pt; font-weight:700; color:{TEXT}; }}
QLabel#Kicker {{ color:{GOLD}; font-size:8.5pt; font-weight:600; letter-spacing:2px; }}
QLabel#Muted {{ color:{MUTED}; }}
QLabel#Faint {{ color:{FAINT}; font-size:9pt; }}
QLabel#SectionTitle {{ font-size:13.5pt; font-weight:600; }}
QLabel#TrackTitle {{ font-size:19pt; font-weight:700; }}
QLabel#CardValue {{ font-size:22pt; font-weight:600; }}
QLabel#StageTitle {{ font-size:12pt; font-weight:700; }}
QLabel#StageHint {{ color:{MUTED}; font-size:9pt; }}
QLabel#Badge {{ color:{GOLD_HI}; background:#2a2416; border:1px solid {GOLD_LO}; border-radius:11px;
               padding:4px 12px; font-size:9pt; }}
QLabel#Good {{ color:{GOOD}; font-weight:600; }}
QLabel#Warn {{ color:{WARN}; font-weight:600; }}

QPushButton {{ background:{CARD_HI}; color:{TEXT}; border:1px solid {LINE}; border-radius:9px;
              padding:9px 16px; font-weight:600; }}
QPushButton:hover {{ background:#2a2f3b; border-color:#4a5163; }}
QPushButton:pressed {{ background:#161920; }}
QPushButton:disabled {{ color:{FAINT}; background:#15171d; border-color:#20232c; }}
QPushButton#Primary, QPushButton#Gold {{
    background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {GOLD_HI}, stop:1 {GOLD});
    color:#17140c; border:0; font-weight:700; }}
QPushButton#Primary:hover, QPushButton#Gold:hover {{
    background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #fbe6b4, stop:1 {GOLD_HI}); }}
QPushButton#Primary:disabled, QPushButton#Gold:disabled {{ background:#3a3424; color:#857a5c; }}
QPushButton#Auto {{
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {GOLD_HI}, stop:0.5 {GOLD}, stop:1 {GOLD_LO});
    color:#14110a; border:0; border-radius:14px; padding:16px 26px; font-size:14pt; font-weight:800;
    letter-spacing:1px; }}
QPushButton#Auto:hover {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #fff0c8, stop:1 {GOLD}); }}
QPushButton#Auto:disabled {{ background:#3a3424; color:#857a5c; }}
QPushButton#Danger {{ background:transparent; color:{BAD}; border:1px solid #6b3434; }}
QPushButton#Danger:hover {{ background:#2a1516; }}
QPushButton#Ghost, QPushButton#Quiet {{ background:transparent; border:1px solid transparent; color:{MUTED};
                                       text-align:left; font-weight:500; }}
QPushButton#Ghost:hover, QPushButton#Quiet:hover {{ color:{TEXT}; border-color:{LINE}; background:{CARD}; }}
QPushButton#Link {{ background:transparent; border:0; color:{GOLD}; padding:2px 0; font-weight:600;
                   text-align:left; }}
QPushButton#Link:hover {{ color:{GOLD_HI}; }}
QPushButton#Nav {{ text-align:left; border:0; background:transparent; color:{MUTED}; padding:12px 16px;
                  border-radius:9px; font-weight:500; }}
QPushButton#Nav:hover {{ background:{CARD}; color:{TEXT}; }}
QPushButton#Nav:checked {{ background:#231f14; color:{GOLD_HI}; border-left:3px solid {GOLD}; }}
QPushButton#Choice {{ background:{CARD}; border:2px solid {LINE}; border-radius:14px; font-size:20pt;
                     font-weight:800; padding:10px; }}
QPushButton#Choice:hover {{ border-color:{GOLD_LO}; }}
QPushButton#Choice:checked {{ background:#231f14; border-color:{GOLD}; color:{GOLD_HI}; }}

QListWidget {{ background:transparent; border:0; outline:0; }}
QListWidget::item {{ padding:11px 12px; border-radius:10px; margin:1px 0; color:{TEXT}; }}
QListWidget::item:hover {{ background:{CARD}; }}
QListWidget::item:selected {{ background:#231f14; color:{GOLD_HI}; border-left:3px solid {GOLD}; }}

QTableWidget, QTreeWidget {{ background:{CARD}; border:1px solid {LINE}; border-radius:12px; gridline-color:{LINE};
    alternate-background-color:#1b1e27; selection-background-color:#2c2717; selection-color:{TEXT}; outline:0; }}
QTableWidget::item {{ padding:7px 8px; border-bottom:1px solid #20232c; }}
QTreeWidget::item {{ padding:9px 7px; }}
QHeaderView::section {{ background:{PANEL}; color:{MUTED}; padding:10px 8px; border:0;
                       border-bottom:1px solid {LINE}; font-weight:600; font-size:9pt; }}
QTableCornerButton::section {{ background:{PANEL}; border:0; }}

QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox, QDoubleSpinBox {{
    background:#10121a; border:1px solid {LINE}; border-radius:8px; padding:7px 9px;
    selection-background-color:{GOLD_LO}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color:{GOLD_LO}; }}
QComboBox::drop-down {{ border:0; width:24px; }}
QComboBox QAbstractItemView {{ background:{CARD_HI}; border:1px solid {LINE}; selection-background-color:#3a3220; }}
QCheckBox {{ spacing:8px; }}
QCheckBox::indicator {{ width:18px; height:18px; border:1px solid #4a5163; border-radius:5px; background:#10121a; }}
QCheckBox::indicator:checked {{ background:{GOLD}; border-color:{GOLD}; }}

QProgressBar {{ background:#1a1d25; border:0; border-radius:5px; min-height:10px; max-height:10px; color:transparent; }}
QProgressBar::chunk {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {GOLD_LO}, stop:1 {GOLD_HI});
                      border-radius:5px; }}
QSlider::groove:horizontal {{ height:4px; background:{LINE}; border-radius:2px; }}
QSlider::sub-page:horizontal {{ background:{GOLD}; border-radius:2px; }}
QSlider::handle:horizontal {{ background:{GOLD_HI}; width:14px; margin:-6px 0; border-radius:7px; }}
QScrollBar:vertical {{ width:9px; background:transparent; margin:0; }}
QScrollBar::handle:vertical {{ background:#343a48; border-radius:4px; min-height:28px; }}
QScrollBar::handle:vertical:hover {{ background:#4a5163; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
QScrollBar:horizontal {{ height:9px; background:transparent; }}
QScrollBar::handle:horizontal {{ background:#343a48; border-radius:4px; min-width:28px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; }}
QSplitter::handle {{ background:{BG}; width:10px; }}
QGroupBox {{ background:{CARD}; border:1px solid {LINE}; border-radius:12px; margin-top:16px; padding:12px; }}
QGroupBox::title {{ subcontrol-origin:margin; left:14px; color:{MUTED}; }}
QTabWidget::pane {{ border:0; }}
QTabBar::tab {{ background:{PANEL}; padding:10px 16px; color:{MUTED}; border-radius:8px; margin-right:4px; }}
QTabBar::tab:selected {{ background:#231f14; color:{GOLD_HI}; }}
QStatusBar {{ background:{PANEL}; color:{MUTED}; border-top:1px solid {LINE}; }}
QMenuBar {{ background:{PANEL}; color:{MUTED}; }}
QMenu {{ background:{CARD_HI}; border:1px solid {LINE}; padding:6px; }}
QMenu::item {{ padding:8px 18px; border-radius:6px; }}
QMenu::item:selected {{ background:#3a3220; color:{GOLD_HI}; }}
QDialog, QMessageBox {{ background:{PANEL}; }}
"""
