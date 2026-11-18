"""«Релиз в один клик» - simple window over ``srm.auto.engine``.

Three steps on one screen: add songs -> check the detected style -> make the release.
Every long action shows a progress bar with percent and the current stage, per song and overall.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from srm import __version__
from srm.auto import engine as E
from srm.core.tools import tools_status

APP_TITLE = "Релиз в один клик"

STYLE = """
QWidget { background:#121516; color:#ece9e1; font-family:'Segoe UI'; font-size:10.5pt; }
QLabel { background:transparent; }
QLabel#Title { font-size:24pt; font-weight:600; color:#f6f2e8; }
QLabel#Sub { color:#9aa5a3; font-size:10.5pt; }
QLabel#Muted { color:#8e9997; }
QLabel#Warn { color:#e8b77a; }
QLabel#Status { color:#d9c3a5; font-size:11pt; }
QFrame#Step { background:#1a1f20; border:1px solid #2b3233; border-radius:12px; }
QFrame#StepActive { background:#272a22; border:1px solid #d9b997; border-radius:12px; }
QFrame#StepDone { background:#1c2621; border:1px solid #3d5a49; border-radius:12px; }
QLabel#StepNum { font-size:15pt; font-weight:700; color:#d9b997; }
QLabel#StepText { font-size:10.5pt; font-weight:600; }
QFrame#Drop { background:#161a1b; border:2px dashed #3a4445; border-radius:14px; }
QFrame#Drop[hot="true"] { border-color:#d9b997; background:#1d1f1a; }
QLabel#DropText { font-size:13pt; color:#c9c4b8; }
QFrame#Card { background:#1a1f20; border:1px solid #2b3233; border-radius:12px; }
QPushButton { background:#242b2c; border:1px solid #3a4344; border-radius:8px; padding:9px 16px; }
QPushButton:hover { background:#2f3939; border-color:#75847f; }
QPushButton:disabled { color:#6a7473; background:#1b2122; border-color:#283031; }
QPushButton#Primary { background:#d9b997; color:#1d1e1b; border-color:#d9b997; font-weight:700;
  font-size:13pt; padding:13px 30px; }
QPushButton#Primary:hover { background:#efd1ae; }
QPushButton#Primary:disabled { background:#4d453c; color:#8f826f; border-color:#4d453c; }
QComboBox { background:#20272a; border:1px solid #3a4344; border-radius:6px; padding:5px 8px; }
QComboBox QAbstractItemView { background:#1d2324; selection-background-color:#3a473f; }
QTableWidget { background:#171b1c; border:1px solid #2b3233; border-radius:10px; gridline-color:#22292a;
  selection-background-color:#2e3a35; alternate-background-color:#1a1f20; }
QTableWidget::item { padding:6px; }
QHeaderView::section { background:#1d2324; color:#93a09b; padding:9px 6px; border:0; font-weight:600; }
QProgressBar { background:#20272a; border:1px solid #333c3d; border-radius:6px; text-align:center;
  color:#f3efe6; font-weight:600; min-height:20px; }
QProgressBar::chunk { background:#b89566; border-radius:5px; }
QProgressBar#Big { min-height:30px; font-size:11.5pt; }
QProgressBar#Done::chunk { background:#4f8a69; }
QProgressBar#Error::chunk { background:#a5524a; }
QPushButton#Genre { padding:6px 10px; font-size:10pt; min-height:26px; }
QPushButton#Genre:checked { background:#3b3326; border:1px solid #d9b997; color:#f6e3c6; font-weight:700; }
QLabel#GenreDesc { color:#c9c4b8; }
QCheckBox { background:transparent; spacing:8px; }
QPlainTextEdit { background:#171b1c; border:1px solid #2b3233; border-radius:10px; padding:8px; }
"""

COL_NO, COL_NAME, COL_STYLE, COL_LEN, COL_LOUD, COL_FIXES, COL_PROGRESS, COL_STATUS = range(8)


def _mmss(s) -> str:
    if not s:
        return "—"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}"


def _eta(seconds: float) -> str:
    if seconds < 60:
        return "меньше минуты"
    return f"≈ {int(round(seconds / 60))} мин"


def _recipe(p: dict) -> str:
    mb = p.get("mb") or {}
    bands = [n for n, k in (("бас", "low"), ("середина", "mid"), ("верх", "high")) if mb.get(k)]
    parts = [f"• многополосная компрессия: {', '.join(bands)}" if bands else "• без многополосной компрессии",
             f"• склейка {p['glue'][0]}:1" if p.get("glue") else "• без общей компрессии (живая динамика)",
             f"• аналоговая окраска {p.get('sat', 0) * 100:.0f}%",
             f"• панч ударных +{p['punch']:.1f} дБ" if p.get("punch") else "• атаки не трогаются",
             f"• стерео шире на {(p.get('width', 1) - 1) * 100:.0f}%",
             f"• бас в моно ниже {p['mono']} Гц, лимитер −1 dBTP"]
    return "\n".join(parts)


def _item(text) -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    return it


# ------------------------------------------------------------------ background work
class Job(QObject):
    progress = Signal(int, float, str)  # row, 0..1, stage text
    row_done = Signal(int, object)
    row_failed = Signal(int, str)
    fixes = Signal(int, object)  # row, running counts of fixed defects
    finished = Signal()

    def __init__(self, kind: str, rows: list[int], paths: list[str], options: dict):
        super().__init__()
        self.kind, self.rows, self.paths, self.options = kind, rows, paths, options
        self.cancel = threading.Event()

    @Slot()
    def run(self):
        for row, path in zip(self.rows, self.paths):
            if self.cancel.is_set():
                break
            try:
                def cb(f, text, row=row):
                    self.progress.emit(row, float(f), text)
                if self.kind == "analyze":
                    res = E.analyze_track(path, progress=cb)
                else:
                    o = self.options[row]
                    res = E.make_release(path, o["out_dir"], style=o["style"], cleanup_level=o["cleanup"],
                                         progress=cb, analysis=o.get("analysis"), auto_style=o.get("auto_style"),
                                         fmt=o.get("fmt", "wav"),
                                         on_fixes=lambda f, row=row: self.fixes.emit(row, f))
                self.row_done.emit(row, res)
            except Exception as exc:  # shown in the row; the other songs continue
                self.row_failed.emit(row, str(exc)[-400:])
        self.finished.emit()


class Step(QFrame):
    def __init__(self, num: int, text: str):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        self.num = QLabel(str(num))
        self.num.setObjectName("StepNum")
        self.text = QLabel(text)
        self.text.setObjectName("StepText")
        lay.addWidget(self.num)
        lay.addWidget(self.text, 1)
        self.set_state("idle")

    def set_state(self, state: str):
        self.setObjectName({"active": "StepActive", "done": "StepDone"}.get(state, "Step"))
        if state == "done":
            self.num.setText("✓")
        self.style().unpolish(self)
        self.style().polish(self)


class DropZone(QFrame):
    dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("Drop")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            self.setProperty("hot", True)
            self.style().polish(self)
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):  # noqa: N802
        self.setProperty("hot", False)
        self.style().polish(self)

    def dropEvent(self, e):  # noqa: N802
        self.setProperty("hot", False)
        self.style().polish(self)
        self.dropped.emit([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])


# ------------------------------------------------------------------ window
class AutoWindow(QMainWindow):
    def __init__(self, paths: list[str] | None = None):
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE} · Suno Release Master {__version__}")
        self.setMinimumSize(1180, 760)
        self.resize(1400, 900)
        self.setStyleSheet(STYLE)
        self.tracks: list[dict] = []  # {path, analysis, result, error, phase}
        self.album: dict = {}
        self.out_dir: Path | None = None
        self.thread: QThread | None = None
        self.job: Job | None = None
        self._t_start = 0.0
        self._player = None
        ini = os.environ.get("SRM_SETTINGS_INI")  # tests keep their choices out of the user's settings
        self.settings = (QSettings(ini, QSettings.IniFormat) if ini
                         else QSettings("SunoReleaseMaster", "ReleaseOneClick"))
        last = str(self.settings.value("last_genre", "auto"))
        self.genre = last if last == "auto" or last in E.PROFILES else "auto"
        self._build()
        self.setAcceptDrops(True)
        if paths:
            self.add_paths(paths)

    # ---- layout
    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(14)
        title = QLabel(APP_TITLE)
        title.setObjectName("Title")
        sub = QLabel("Добавьте песни из Suno и выберите жанр — программа уберёт артефакты ИИ, сделает мастеринг "
                     "под жанр и сохранит готовые к релизу WAV или MP3. Исходные файлы не изменяются.")
        sub.setObjectName("Sub")
        sub.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(sub)

        steps = QHBoxLayout()
        self.steps = [Step(1, "Добавьте песни"), Step(2, "Выберите жанр"), Step(3, "Получите релиз")]
        for s in self.steps:
            steps.addWidget(s)
        lay.addLayout(steps)
        lay.addWidget(self._build_genres())

        # drop zone (visible while the list is empty)
        self.drop = DropZone()
        self.drop.dropped.connect(self.add_paths)
        dl = QVBoxLayout(self.drop)
        dl.setContentsMargins(30, 40, 30, 40)
        t = QLabel("Перетащите сюда папку с песнями или отдельные файлы")
        t.setObjectName("DropText")
        t.setAlignment(Qt.AlignCenter)
        hint = QLabel("WAV, MP3, M4A, FLAC — можно весь альбом сразу")
        hint.setObjectName("Muted")
        hint.setAlignment(Qt.AlignCenter)
        br = QHBoxLayout()
        br.addStretch()
        b1 = QPushButton("Выбрать папку…")
        b1.clicked.connect(self.pick_folder)
        b2 = QPushButton("Выбрать файлы…")
        b2.clicked.connect(self.pick_files)
        br.addWidget(b1)
        br.addWidget(b2)
        br.addStretch()
        dl.addWidget(t)
        dl.addWidget(hint)
        dl.addSpacing(10)
        dl.addLayout(br)
        lay.addWidget(self.drop, 1)

        # list + details
        self.body = QWidget()
        bl = QHBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(14)
        left = QVBoxLayout()
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["№", "Песня", "Стиль", "Длина", "Громкость, LUFS", "Убрано",
                                              "Прогресс", "Статус"])
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().setDefaultSectionSize(40)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        for c, w in ((COL_NO, 36), (COL_STYLE, 165), (COL_LEN, 54), (COL_LOUD, 128), (COL_FIXES, 72),
                     (COL_PROGRESS, 100), (COL_STATUS, 175)):
            h.setSectionResizeMode(c, QHeaderView.Fixed)
            self.table.setColumnWidth(c, w)
        self.table.itemSelectionChanged.connect(self.show_details)
        left.addWidget(self.table, 1)
        self.album_label = QLabel("")
        self.album_label.setWordWrap(True)
        left.addWidget(self.album_label)
        opt = QHBoxLayout()
        opt.addWidget(QLabel("Очистка артефактов ИИ:"))
        self.cleanup = QComboBox()
        for k, v in E.CLEANUP_LABELS.items():
            self.cleanup.addItem(v, k)
        self.cleanup.setCurrentIndex(1)
        opt.addWidget(self.cleanup)
        opt.addSpacing(14)
        opt.addWidget(QLabel("Формат:"))
        self.fmt = QComboBox()
        for k, v in E.FORMATS.items():
            self.fmt.addItem(v, k)
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(str(self.settings.value("format", "wav")))))
        self.fmt.currentIndexChanged.connect(lambda _i: self.settings.setValue("format", self.fmt.currentData()))
        opt.addWidget(self.fmt)
        add = QPushButton("+ Добавить ещё")
        add.clicked.connect(self.pick_files)
        clear = QPushButton("Очистить список")
        clear.clicked.connect(self.clear)
        self.add_btn, self.clear_btn = add, clear
        opt.addStretch()
        opt.addWidget(add)
        opt.addWidget(clear)
        left.addLayout(opt)
        dest = QHBoxLayout()
        dest.addWidget(QLabel("Сохранять в:"))
        self.dest_label = QLabel("")
        self.dest_label.setObjectName("Muted")
        dest.addWidget(self.dest_label, 1)
        pick = QPushButton("Выбрать папку…")
        pick.clicked.connect(self.pick_dest)
        self.dest_reset = QPushButton("Рядом с песнями")
        self.dest_reset.setToolTip("Папка «РЕЛИЗ» рядом с исходными песнями")
        self.dest_reset.clicked.connect(lambda: self.set_dest(""))
        self.dest_pick = pick
        dest.addWidget(pick)
        dest.addWidget(self.dest_reset)
        left.addLayout(dest)
        bl.addLayout(left, 3)

        side = QFrame()
        side.setObjectName("Card")
        side.setFixedWidth(340)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 14, 16, 14)
        self.d_title = QLabel("Выберите песню")
        self.d_title.setStyleSheet("font-size:13pt; font-weight:600;")
        self.d_title.setWordWrap(True)
        sl.addWidget(self.d_title)
        self.d_text = QPlainTextEdit()
        self.d_text.setReadOnly(True)
        sl.addWidget(self.d_text, 1)
        sl.addWidget(QLabel("Послушать (громкость выровнена):"))
        pr = QHBoxLayout()
        self.play_src = QPushButton("▶ Было")
        self.play_src.clicked.connect(lambda: self.play("src"))
        self.play_out = QPushButton("▶ Стало")
        self.play_out.clicked.connect(lambda: self.play("out"))
        self.stop_btn = QPushButton("■")
        self.stop_btn.setFixedWidth(46)
        self.stop_btn.clicked.connect(self.stop)
        pr.addWidget(self.play_src)
        pr.addWidget(self.play_out)
        pr.addWidget(self.stop_btn)
        sl.addLayout(pr)
        bl.addWidget(side)
        self.body.hide()
        lay.addWidget(self.body, 1)

        # bottom: overall progress + main action
        bottom = QFrame()
        bottom.setObjectName("Card")
        bot = QVBoxLayout(bottom)
        bot.setContentsMargins(18, 14, 18, 14)
        self.status = QLabel("Добавьте песни, чтобы начать.")
        self.status.setObjectName("Status")
        self.total = QProgressBar()
        self.total.setObjectName("Big")
        self.total.setRange(0, 1000)
        self.total.setValue(0)
        self.total.setFormat("0%")
        bot.addWidget(self.status)
        bot.addWidget(self.total)
        row = QHBoxLayout()
        self.fix_total = QLabel("")
        self.fix_total.setStyleSheet("font-size:12pt; font-weight:600; color:#8fd1a7;")
        row.addWidget(self.fix_total)
        self.ffmpeg_note = QLabel("")
        self.ffmpeg_note.setObjectName("Warn")
        row.addWidget(self.ffmpeg_note, 1)
        self.open_out = QPushButton("Открыть папку релиза")
        self.open_out.clicked.connect(lambda: self.out_dir and QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.out_dir))))
        self.open_out.hide()
        self.cancel_btn = QPushButton("Остановить")
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.hide()
        self.go = QPushButton("Сделать релиз")
        self.go.setObjectName("Primary")
        self.go.setEnabled(False)
        self.go.clicked.connect(self.start_release)
        row.addWidget(self.open_out)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.go)
        bot.addLayout(row)
        lay.addWidget(bottom)
        self._set_steps(0)
        self.set_dest(str(self.settings.value("dest", "")), save=False)
        if tools_status().get("status") != "AVAILABLE":
            self.ffmpeg_note.setText("FFmpeg не найден — без него программа не читает аудио. "
                                     "Установите командой: winget install Gyan.FFmpeg")

    def _build_genres(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel("Жанр для мастеринга")
        t.setStyleSheet("font-size:12pt; font-weight:600;")
        head.addWidget(t)
        head.addStretch()
        self.apply_all = QCheckBox("Применить ко всем песням")
        self.apply_all.setChecked(True)
        self.apply_all.setToolTip("Снимите галочку, чтобы задать жанр только выделенным песням "
                                  "(выделение: Ctrl или Shift + клик)")
        head.addWidget(self.apply_all)
        v.addLayout(head)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.genre_btns: dict[str, QPushButton] = {}
        keys = ["auto"] + list(E.PROFILES)  # Whale Jazz is always the last button
        for i, k in enumerate(keys):
            if k == "auto":
                b = QPushButton("Авто")
                b.setToolTip("Программа сама определит жанр по звуку и по тексту описания из Suno")
            else:
                pr = E.PROFILES[k]
                b = QPushButton(pr["short"].replace("&", "&&"))  # "&" would become a shortcut underline
                b.setToolTip(f"<b>{pr['label']}</b><br>{pr['desc']}<br>Громкость релиза: {pr['lufs']} LUFS")
            b.setObjectName("Genre")
            b.setCheckable(True)
            b.clicked.connect(lambda _c=False, k=k: self.choose_genre(k))
            grid.addWidget(b, i // 6, i % 6)
            self.genre_btns[k] = b
        v.addLayout(grid)
        self.genre_desc = QLabel("")
        self.genre_desc.setObjectName("GenreDesc")
        self.genre_desc.setWordWrap(True)
        v.addWidget(self.genre_desc)
        self._mark_genre(self.genre)
        return card

    def _mark_genre(self, key: str):
        for k, b in self.genre_btns.items():
            b.setChecked(k == key)
        if key == "auto":
            self.genre_desc.setText("<b>Авто</b> — жанр определяется по звуку и по тексту описания из Suno "
                                    "(если там назван один из ваших жанров, он будет выбран точно).")
        else:
            pr = E.PROFILES[key]
            self.genre_desc.setText(f"<b>{pr['label']}</b> — {pr['desc']} "
                                    f"<span style='color:#8e9997'>Громкость {pr['lufs']} LUFS.</span>")

    def choose_genre(self, key: str):
        """Genre button: set it for all songs (or the selected ones) and remember it for next time."""
        self.genre = key
        self.settings.setValue("last_genre", key)
        self._mark_genre(key)
        if self.busy() and getattr(self, "_job_kind", "") == "release":
            return  # the running release keeps its settings; the choice is remembered for the next one
        if self.apply_all.isChecked():
            rows = range(len(self.tracks))
        else:
            rows = sorted({i.row() for i in self.table.selectedIndexes()}) or range(len(self.tracks))
        for r in rows:
            self._set_row_style(r, key)
        self.show_details()

    def _set_row_style(self, r: int, key: str):
        self.tracks[r]["style"] = key
        combo = self.table.cellWidget(r, COL_STYLE)
        combo.blockSignals(True)
        combo.setCurrentIndex(max(0, combo.findData(key)))
        combo.blockSignals(False)

    def pick_dest(self):
        d = QFileDialog.getExistingDirectory(self, "Куда сохранять готовые песни", self.dest or "")
        if d:
            self.set_dest(d)

    def set_dest(self, path: str, save: bool = True):
        """'' = folder РЕЛИЗ next to each song; otherwise one folder for everything (remembered)."""
        self.dest = path if path and Path(path).is_dir() else ""
        if save:
            self.settings.setValue("dest", self.dest)
        self.dest_label.setText(self.dest or "папка «РЕЛИЗ» рядом с песнями")
        self.dest_reset.setEnabled(bool(self.dest))
        if self.tracks:
            self.out_dir = self._dest_for(0)

    def _dest_for(self, r: int) -> Path:
        return Path(self.dest) if self.dest else Path(self.tracks[r]["path"]).parent / E.OUT_DIR

    def _set_steps(self, current: int):
        for i, s in enumerate(self.steps):
            s.set_state("done" if i < current else "active" if i == current else "idle")

    # ---- adding songs
    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802
        self.add_paths([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Папка с песнями")
        if d:
            self.add_paths([d])

    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Песни", "", "Аудио (*.wav *.mp3 *.m4a *.flac *.aiff *.ogg *.opus)")
        if files:
            self.add_paths(files)

    def add_paths(self, paths: list[str]):
        if self.busy():
            return
        known = {t["path"] for t in self.tracks}
        new = [str(p) for p in E.list_audio(paths) if str(p) not in known]
        if not new:
            QMessageBox.information(self, APP_TITLE, "В выбранном месте нет аудиофайлов (WAV, MP3, M4A, FLAC).")
            return
        for p in new:
            self.tracks.append({"path": p, "analysis": None, "result": None, "error": None, "phase": "wait",
                                "style": self.genre})
            self._add_row(len(self.tracks) - 1)
            self._set_row_style(len(self.tracks) - 1, self.genre)
        self.drop.hide()
        self.body.show()
        self.out_dir = self._dest_for(0)
        self.run_job("analyze", [i for i, t in enumerate(self.tracks) if t["analysis"] is None])

    def _add_row(self, r: int):
        t = self.tracks[r]
        self.table.insertRow(r)
        self.table.setItem(r, COL_NO, _item(r + 1))
        self.table.setItem(r, COL_NAME, _item(Path(t["path"]).stem))
        combo = QComboBox()
        combo.addItem("Авто — определяем…", "auto")
        for k in E.PROFILES:
            combo.addItem(E.PROFILES[k]["short"], k)
        combo.currentIndexChanged.connect(lambda _i, r=r, c=combo: self._style_changed(r, c))
        self.table.setCellWidget(r, COL_STYLE, combo)
        self.table.setItem(r, COL_LEN, _item("—"))
        self.table.setItem(r, COL_LOUD, _item("—"))
        self.table.setItem(r, COL_FIXES, _item("—"))
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setFormat("%p%")
        self.table.setCellWidget(r, COL_PROGRESS, bar)
        self.table.setItem(r, COL_STATUS, _item("В очереди"))

    def _style_changed(self, r: int, combo: QComboBox):
        self.tracks[r]["style"] = combo.currentData()
        self.show_details()

    def clear(self):
        if self.busy():
            return
        self.stop()
        self.tracks, self.album = [], {}
        self.table.setRowCount(0)
        self.body.hide()
        self.drop.show()
        self.go.setEnabled(False)
        self.open_out.hide()
        self.status.setText("Добавьте песни, чтобы начать.")
        self._bar(self.total, 0, "")
        self._set_steps(0)

    # ---- jobs
    def busy(self) -> bool:
        return self.thread is not None

    def run_job(self, kind: str, rows: list[int]):
        if not rows:
            self._after_job(kind)
            return
        options = {}
        if kind == "release":
            for r in rows:
                t = self.tracks[r]
                options[r] = {"out_dir": str(self._dest_for(r)), "style": t["style"], "fmt": self.fmt.currentData(),
                              "cleanup": self.cleanup.currentData(), "analysis": t["analysis"],
                              "auto_style": self._auto_style_for(r)}
        for r in rows:
            self.tracks[r]["phase"] = kind
            self.tracks[r]["error"] = None
            bar = self.table.cellWidget(r, COL_PROGRESS)
            bar.setObjectName("")
            bar.style().polish(bar)
            self._bar(bar, 0)
            self.table.item(r, COL_STATUS).setText("В очереди")
        self._job_rows, self._job_kind = rows, kind
        self._t_start = time.monotonic()
        self.thread = QThread(self)
        self.job = Job(kind, rows, [self.tracks[r]["path"] for r in rows], options)
        self.job.moveToThread(self.thread)
        self.thread.started.connect(self.job.run)
        self.job.progress.connect(self.on_progress, Qt.QueuedConnection)
        self.job.row_done.connect(self.on_row_done, Qt.QueuedConnection)
        self.job.row_failed.connect(self.on_row_failed, Qt.QueuedConnection)
        self.job.fixes.connect(self.on_fixes, Qt.QueuedConnection)
        self.job.finished.connect(self.on_finished, Qt.QueuedConnection)
        self.job.finished.connect(self.thread.quit)
        self.thread.start()
        self.go.setEnabled(False)
        self.cancel_btn.show()
        self.add_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)
        self.cleanup.setEnabled(False)
        self.fmt.setEnabled(False)
        self.dest_pick.setEnabled(False)
        self.dest_reset.setEnabled(False)
        if kind == "release":
            for r in rows:
                self.tracks[r]["fixes"] = {}
                self.table.item(r, COL_FIXES).setText("—")
            self._update_fix_total()

    @Slot(int, object)
    def on_fixes(self, row: int, f: dict):
        """Live: the counts appear while the song is still being processed."""
        self.tracks[row]["fixes"] = f
        it = self.table.item(row, COL_FIXES)
        it.setText(str(E.fixes_total(f)))
        it.setToolTip(E.fixes_text(f))
        self._update_fix_total()
        if self.table.currentRow() == row:
            self.show_details()

    def _update_fix_total(self):
        tot = sum(E.fixes_total(t.get("fixes") or {}) for t in self.tracks)
        self.fix_total.setText(f"Убрано дефектов: {tot}" if tot else "")

    def cancel(self):
        if self.job:
            self.job.cancel.set()
            self.status.setText("Останавливаем после текущей песни…")

    @staticmethod
    def _bar(bar: QProgressBar, value: float, fmt: str | None = None):
        bar.setValue(int(round(value * bar.maximum())))
        bar.setFormat(fmt if fmt is not None else f"{int(round(value * 100))}%")

    @Slot(int, float, str)
    def on_progress(self, row: int, frac: float, text: str):
        t = self.tracks[row]
        if t["phase"] != self._job_kind:
            return  # late message from a finished step
        self._bar(self.table.cellWidget(row, COL_PROGRESS), frac)
        self.table.item(row, COL_STATUS).setText(text)
        i = self._job_rows.index(row)
        n = len(self._job_rows)
        overall = (i + frac) / n
        verb = "Анализ" if self._job_kind == "analyze" else "Обработка"
        elapsed = time.monotonic() - self._t_start
        left = f" · осталось {_eta(elapsed / overall * (1 - overall))}" if overall > 0.03 else ""
        self._bar(self.total, overall, f"{int(overall * 100)}%  ·  {verb}: песня {i + 1} из {n}")
        self.status.setText(f"{verb} · {Path(t['path']).stem} — {text}{left}")

    @Slot(int, object)
    def on_row_done(self, row: int, res: dict):
        t = self.tracks[row]
        bar = self.table.cellWidget(row, COL_PROGRESS)
        bar.setObjectName("Done")
        bar.style().polish(bar)
        self._bar(bar, 1.0, "100%")
        if self._job_kind == "analyze":
            t["analysis"], t["phase"] = res, "analyzed"
            self.table.item(row, COL_LEN).setText(_mmss(res.get("duration_s")))
            lufs = res.get("lufs")
            self.table.item(row, COL_LOUD).setText("—" if lufs is None else f"{lufs:.1f}")
            self.table.item(row, COL_STATUS).setText("Готова к обработке")
        else:
            t["result"], t["phase"] = res, "done"
            b, a = res["before"]["lufs"], res["after"]["lufs"]
            self.table.item(row, COL_LOUD).setText(f"{b:.1f} → {a:.1f}" if b is not None and a is not None else "—")
            self.table.item(row, COL_STATUS).setText("✓ Готово")
        if self.table.currentRow() == row or self.table.currentRow() < 0:
            self.table.selectRow(row)
        self.show_details()

    @Slot(int, str)
    def on_row_failed(self, row: int, msg: str):
        t = self.tracks[row]
        t["error"], t["phase"] = msg, "error"
        bar = self.table.cellWidget(row, COL_PROGRESS)
        bar.setObjectName("Error")
        bar.style().polish(bar)
        self._bar(bar, 1.0, "ошибка")
        self.table.item(row, COL_STATUS).setText("✗ Ошибка — см. справа")
        self.show_details()

    @Slot()
    def on_finished(self):
        kind = self._job_kind
        cancelled = self.job.cancel.is_set() if self.job else False
        self.thread = None
        self.job = None
        self.cancel_btn.hide()
        self.add_btn.setEnabled(True)
        self.clear_btn.setEnabled(True)
        self.cleanup.setEnabled(True)
        self.fmt.setEnabled(True)
        self.dest_pick.setEnabled(True)
        self.dest_reset.setEnabled(bool(self.dest))
        for r in self._job_rows:  # rows that never started after a stop
            if self.tracks[r]["phase"] == kind:
                self.tracks[r]["phase"] = "wait" if kind == "analyze" else "analyzed"
                self.table.item(r, COL_STATUS).setText("Остановлено")
        if cancelled:
            self.status.setText("Остановлено.")
        self._after_job(kind)

    def _after_job(self, kind: str):
        ready = [t for t in self.tracks if t["analysis"]]
        if kind == "analyze":
            self.album = E.album_style([t["analysis"] for t in ready])
            self._refresh_styles()
            self._bar(self.total, 0, "0%")
            fails = sum(1 for t in self.tracks if t["phase"] == "error")
            self.status.setText(
                f"Анализ готов, песен: {len(ready)}" + (f", не прочитано: {fails}" if fails else "") +
                ". Выберите жанр кнопкой сверху и нажмите «Сделать релиз».")
            self.go.setEnabled(bool(ready))
            self._set_steps(1)
            self.show_details()
        else:
            done = [t for t in self.tracks if t["result"]]
            fails = [t for t in self.tracks if t["phase"] == "error"]
            if done:
                for d in {Path(t["result"]["output"]).parent for t in done}:
                    E.write_report([t["result"] for t in done if Path(t["result"]["output"]).parent == d], d)
                self.out_dir = Path(done[0]["result"]["output"]).parent
                self.open_out.show()
            self._bar(self.total, 1.0 if done else 0, f"100%  ·  готово {len(done)} из {len(self.tracks)}")
            tot = sum(t["result"].get("fixes_total", 0) for t in done)
            self.status.setText(
                f"Готово песен: {len(done)}, убрано дефектов: {tot}. Файлы — в папке «{self.out_dir}», "
                f"там же ОТЧЁТ.txt."
                + (f" Ошибок: {len(fails)}." if fails else ""))
            self.go.setEnabled(True)
            self.go.setText("Сделать релиз ещё раз")
            self._set_steps(3 if done else 1)

    def _auto_style_for(self, r: int) -> str:
        det = self.tracks[r]["analysis"]["det"]
        if det["confidence"] == "ВЫСОКАЯ" and det["declared"]:
            return det["style"]
        return self.album.get("style") or det["style"]

    def _refresh_styles(self):
        a = self.album
        if a:
            self.album_label.setText(
                f"Авто-определение для альбома: <b>{E.style_label(a['style'])}</b> "
                f"<span style='color:#8e9997'>({a.get('source', '')}, уверенность {a['confidence'].lower()})</span>")
        for r, t in enumerate(self.tracks):
            combo = self.table.cellWidget(r, COL_STYLE)
            if t["analysis"]:
                combo.setItemText(0, f"Авто — {E.PROFILES[self._auto_style_for(r)]['short']}")
            elif t["phase"] == "error":
                combo.setItemText(0, "Авто")

    def start_release(self):
        rows = [i for i, t in enumerate(self.tracks) if t["analysis"]]
        if not rows:
            return
        self.stop()
        self._set_steps(2)
        self.go.setText("Сделать релиз")
        self.run_job("release", rows)

    # ---- details + listening
    def show_details(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(self.tracks):
            return
        t = self.tracks[r]
        self._mark_genre(t["style"])
        self.d_title.setText(Path(t["path"]).stem)
        lines = []
        if t["error"]:
            lines += ["Ошибка:", t["error"]]
        a = t["analysis"]
        if a:
            det = a["det"]
            meas = a["meas"]
            tempo = (meas.get("tempo") or {}).get("bpm")
            chosen = t["style"] if t["style"] != "auto" else self._auto_style_for(r)
            prof = E.PROFILES[chosen]
            lines += [f"Стиль для мастеринга: {prof['label']}" + (" (вручную)" if t["style"] != "auto" else ""),
                      f"По звуку похоже на: {det.get('guess_label', '—')} (уверенность {det['confidence'].lower()})",
                      f"Темп: {tempo:.0f} BPM" if tempo else "Темп: —",
                      f"Громкость сейчас: {a['lufs']} LUFS → цель {prof['lufs']} LUFS",
                      "", "Рецепт мастеринга:", _recipe(prof)]
            if a["lossy"]:
                lines += ["", "⚠ Исходник сжатый (MP3/M4A). Лучше скачать из Suno WAV — качество релиза будет выше."]
        f = t.get("fixes") or (t["result"] or {}).get("fixes")
        if f:
            lines += ["", f"Убрано дефектов: {E.fixes_total(f)}"] + [
                f"  • {E.FIX_NAMES[k]}: {f[k]}" for k in E.FIX_NAMES if f.get(k)]
        res = t["result"]
        if res:
            lines += ["", "Что сделано:"] + [f"• {x}" for x in res["actions"]]
            lines += ["", "Файлы: " + ", ".join(Path(o).name for o in res.get("outputs") or [res["output"]]),
                      res["format"],
                      f"Пик: {res['after']['true_peak']} dBTP · время обработки {res['seconds']:.0f} с"]
        elif a:
            lines += ["", "Нажмите «Сделать релиз» — здесь появится список того, что сделано."]
        self.d_text.setPlainText("\n".join(lines))
        self.play_out.setEnabled(bool(res))
        self.play_src.setEnabled(True)

    def play(self, which: str):
        r = self.table.currentRow()
        if r < 0:
            return
        t = self.tracks[r]
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        except Exception:
            QMessageBox.information(self, APP_TITLE, "Прослушивание недоступно на этом компьютере.")
            return
        if self._player is None:
            self._out = QAudioOutput()
            self._player = QMediaPlayer()
            self._player.setAudioOutput(self._out)
            self._pending_pos = 0

            def loaded(status, p=self._player):
                if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and self._pending_pos:
                    p.setPosition(self._pending_pos)
                    self._pending_pos = 0
            self._player.mediaStatusChanged.connect(loaded)
        pos = self._player.position() if self._player.playbackState() == QMediaPlayer.PlayingState else 0
        path = t["path"] if which == "src" else t["result"]["output"]
        # loudness matching: the louder file is turned down, so the difference you hear is the sound, not volume
        b = (t["analysis"] or {}).get("lufs")
        a = (t["result"] or {}).get("after", {}).get("lufs")
        vol = 1.0
        if b is not None and a is not None:
            louder = max(a, b)
            vol = 10 ** (((b if which == "src" else a) - louder) / 20)
        self._out.setVolume(max(0.05, min(1.0, vol)))
        self._pending_pos = pos  # same moment of the song after switching
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()
        self.status.setText(f"Играет: {'было' if which == 'src' else 'стало'} — {Path(t['path']).stem}")

    def stop(self):
        if self._player is not None:
            self._player.stop()

    def closeEvent(self, e):  # noqa: N802
        if self.job:
            self.job.cancel.set()
        if self.thread is not None:
            self.thread.wait(120000)
        self.stop()
        super().closeEvent(e)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("SunoReleaseMaster")
    w = AutoWindow(argv or None)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
