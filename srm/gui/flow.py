"""Main window: the release workflow as ordered stages for a whole album, plus one AUTO button.

Open a folder -> every album in it appears on the left (an album folder gives one album). For the
selected album the stages run in order - Анализ, Версии, Ремонт, Мастеринг, Прослушать, Релиз - each
by its own button, or all at once with АВТО. Thin layer over ``srm.service.pipeline``.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from srm import __version__
from srm.core.paths import safe_name
from srm.core.tools import get_tools, tools_status
from srm.dsp.presets import PRESETS
from srm.gui import theme as T
from srm.service.pipeline import SERVICE_DIRS, STAGE_NAMES, STAGES, STYLE_LABELS

APP_NAME = "Music Release Master"
FFMPEG_CMD = "winget install Gyan.FFmpeg"
STAGE_UI = [
    # key, title, hint, button, tooltip
    ("analyze", "Анализ", "Проверяет звук каждого трека: громкость, пики, динамика, повреждения.",
     "Проверить", "Ничего не меняет в файлах. Нужен для всех следующих шагов."),
    ("versions", "Версии", "Если у песни несколько вариантов — оставляет технически лучший.",
     "Выбрать лучшие", "Остальные варианты не удаляются и не перемещаются: они просто не попадут в релиз."),
    ("repair", "Ремонт", "Убирает одиночные щелчки и резкие обрывы. Музыку и ритм не трогает.",
     "Исправить", "Каждая правка проверяется автоматически. Исходные файлы не изменяются."),
    ("master", "Мастеринг", "Релизное звучание: громкость по стилю и защита от перегрузки.",
     "Сделать мастер", "Создаёт новую WAV-версию каждого трека в папке проекта."),
    ("listen", "Прослушать", "Сравните оригинал и мастер на одинаковой громкости.",
     "Открыть плеер", "Мгновенное переключение «оригинал / мастер» в одной точке трека."),
    ("release", "Релиз", "Собирает пакет: WAV-мастера, MP3 с обложкой, отчёт проверки.",
     "Собрать релиз", "Пакет сохраняется в папке проекта. Ничего никуда не отправляется."),
]


def human_error(msg: str) -> str:
    """Engine errors in words a musician understands."""
    low = (msg or "").lower()
    if "ffmpeg" in low and ("not found" in low or "ffprobe" in low):
        return f"не найден FFmpeg — установите его командой: {FFMPEG_CMD}"
    if "source changed" in low:
        return "файл изменился после чтения папки — нажмите «Обновить папку»"
    if "source missing" in low:
        return "файл не найден на диске"
    if "low disk space" in low:
        return "мало свободного места на диске"
    if "probe failed" in low or "decode failed" in low or "decoded zero" in low:
        return "файл повреждён или не читается"
    if "would clip" in low:
        return "после обработки получилась перегрузка — попробуйте другой стиль альбома"
    if "no approved rendered version" in low or "cannot export" in low:
        return "не у всех треков есть мастер — сначала выполните шаг «Мастеринг»"
    return msg


def data_dir() -> Path:
    base = Path(QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation) or Path.home())
    d = base / "SunoReleaseMaster"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Task(QObject):
    """Background work with progress. Signals are delivered to the window on the GUI thread."""

    progress = Signal(str, int, int, str)
    stage_done = Signal(dict)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.cancel = threading.Event()

    @Slot()
    def run(self):
        try:
            self.finished.emit(self.fn(self))
        except Exception as exc:  # shown to the user in plain words
            self.failed.emit(str(exc))


class StageCard(QFrame):
    """One step of the workflow: number, name, what it does, how far it is, and its own button."""

    def __init__(self, number: int, key: str, title: str, hint: str, action: str, tip: str):
        super().__init__()
        self.key = key
        self.color = T.STAGE_COLORS[key]
        self.setObjectName("StageCard")
        self.setMinimumHeight(212)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # six equal columns
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)
        head = QHBoxLayout()
        num = QLabel(str(number))
        num.setFixedSize(26, 26)
        num.setAlignment(Qt.AlignCenter)
        num.setStyleSheet(f"background:{self.color}; color:#0c0d11; border-radius:13px; font-weight:800;")
        name = QLabel(title)
        name.setObjectName("StageTitle")
        head.addWidget(num)
        head.addWidget(name, 1)
        lay.addLayout(head)
        text = QLabel(hint)
        text.setObjectName("StageHint")
        text.setWordWrap(True)
        text.setMinimumHeight(68)
        text.setAlignment(Qt.AlignTop)
        lay.addWidget(text)
        lay.addStretch()
        self.state = QLabel("—")
        self.state.setStyleSheet(f"color:{T.MUTED}; font-weight:600;")
        lay.addWidget(self.state)
        self.button = QPushButton(action)
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.setStyleSheet(T.stage_button_css(self.color))
        self.button.setToolTip(tip)
        lay.addWidget(self.button)
        self.extra = QPushButton("")
        self.extra.setObjectName("Link")
        self.extra.setCursor(Qt.PointingHandCursor)
        self.extra.hide()
        lay.addWidget(self.extra)
        self.setToolTip(tip)

    def set_state(self, text: str, done: bool):
        self.state.setText(text)
        self.state.setStyleSheet(f"color:{T.GOOD if done else T.MUTED}; font-weight:600;")
        self.setProperty("done", "true" if done else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class ListenDialog(QDialog):
    """Original against master: instant switching at the same position, loudness matched."""

    def __init__(self, parent, engine, track_ids: list[int], start_id: int):
        super().__init__(parent)
        from srm.versus.gui import WaveView
        from srm.versus.player import MixerCore

        self.engine, self.ids, self.index = engine, track_ids, max(0, track_ids.index(start_id))
        self.setWindowTitle("Прослушать: оригинал и мастер")
        self.resize(980, 460)
        self.core = MixerCore()
        self.sink = None
        self._threads: list = []
        self._loading: int | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(14)
        self.title = QLabel("")
        self.title.setObjectName("TrackTitle")
        self.note = QLabel("")
        self.note.setObjectName("Muted")
        self.note.setWordWrap(True)
        lay.addWidget(self.title)
        lay.addWidget(self.note)
        row = QHBoxLayout()
        self.btn_src = QPushButton("ОРИГИНАЛ")
        self.btn_master = QPushButton("МАСТЕР")
        for b, key in ((self.btn_src, "src"), (self.btn_master, "master")):
            b.setObjectName("Choice")
            b.setCheckable(True)
            b.setMinimumHeight(64)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda checked=False, k=key: self.switch(k))
            row.addWidget(b)
        self.btn_src.setToolTip("Клавиша 1")
        self.btn_master.setToolTip("Клавиша 2")
        lay.addLayout(row)
        self.wave = WaveView()
        self.wave.seek.connect(lambda f: self.core.seek(int(f * self.core.length())))
        lay.addWidget(self.wave, 1)
        bar = QHBoxLayout()
        self.play = QPushButton("▶  Слушать")
        self.play.setObjectName("Gold")
        self.play.clicked.connect(self.toggle)
        self.time = QLabel("0:00")
        self.time.setObjectName("SectionTitle")
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(170)
        self.volume.valueChanged.connect(lambda v: setattr(self.core, "volume", v / 100.0))
        self.approve = QPushButton("Одобрить этот трек")
        self.approve.setStyleSheet(T.stage_button_css(T.GOOD))
        self.approve.setToolTip("Записывает, что вы прослушали мастер и он вас устраивает.")
        self.approve.clicked.connect(self.approve_track)
        self.next = QPushButton("Следующий трек  →")
        self.next.clicked.connect(lambda: self.load((self.index + 1) % len(self.ids)))
        bar.addWidget(self.play)
        bar.addWidget(self.time)
        bar.addStretch()
        bar.addWidget(QLabel("Громкость"))
        bar.addWidget(self.volume)
        bar.addSpacing(18)
        bar.addWidget(self.approve)
        bar.addWidget(self.next)
        lay.addLayout(bar)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(60)
        self.load(self.index)

    def load(self, index: int):
        from srm.gui.app import run_bg
        from srm.versus.core import load_playback

        self.core.playing = False
        self.index = index
        tid = self.ids[index]
        t = self.engine.track(tid)
        lv = self.engine.db.one("SELECT version_no FROM versions WHERE track_id=? AND status IN ('RENDERED','APPROVED') "
                                "ORDER BY version_no DESC LIMIT 1", (tid,))
        self.title.setText(f"{t['title']}   ·   {index + 1} из {len(self.ids)}")
        self.note.setText("Загружаем…")
        self._loading = tid
        v = self.engine.version(tid, lv["version_no"])
        a = self.engine.get_analysis(tid)
        src_lufs = a["measurements"]["loudness"]["integrated_lufs"] if a else None
        out_lufs = ((v["report"] or {}).get("output_measurements") or {}).get("loudness", {}).get("integrated_lufs")
        self._version = (tid, lv["version_no"], v["status"])

        def work():
            return {"src": load_playback(t["path"]), "master": load_playback(v["output_path"])}

        def done(tracks):
            if self._loading != tid:
                return
            gains = {"src": 0.0, "master": 0.0}
            if src_lufs is not None and out_lufs is not None:  # turn the louder one down: fair comparison
                gains["src" if src_lufs > out_lufs else "master"] = -abs(src_lufs - out_lufs)
            self.core.set_tracks(tracks, gains)
            self.core.volume = self.volume.value() / 100.0
            self.core.switch("master")
            self.wave.peaks = (a or {}).get("measurements", {}).get("timelines", {}).get("waveform", {}).get("peak", [])
            self._mark()
            diff = "" if src_lufs is None or out_lufs is None else (
                f"Оригинал {src_lufs:.1f} LUFS, мастер {out_lufs:.1f} LUFS. ")
            self.note.setText(diff + "При прослушивании громкость выровнена, чтобы «громче» не казалось «лучше». "
                              "Переключайте в любой момент — позиция сохраняется.")
            self.approve.setEnabled(self._version[2] != "APPROVED")
            self.approve.setText("Одобрен ✓" if self._version[2] == "APPROVED" else "Одобрить этот трек")
            self._ensure_audio()

        run_bg(self, work, on_done=done, on_fail=lambda m: self.note.setText("Не удалось загрузить: " + human_error(m)))

    def _ensure_audio(self):
        if self.sink is not None:
            return
        try:
            from srm.versus.player import start_sink

            self.sink, self.device = start_sink(self.core, self)
        except Exception as exc:
            self.note.setText(f"Воспроизведение недоступно: {exc}")

    def _mark(self):
        self.btn_src.setChecked(self.core.active == "src")
        self.btn_master.setChecked(self.core.active == "master")

    def switch(self, key: str):
        self.core.switch(key)
        self._mark()

    def toggle(self):
        if not self.core.tracks:
            return
        if not self.core.playing and self.core.pos >= self.core.length():
            self.core.seek(0)
        self.core.playing = not self.core.playing

    def approve_track(self):
        tid, vno, _ = self._version
        try:
            self.engine.listening_signoff(tid, vno, note="Прослушано в плеере «оригинал / мастер»")
            self.engine.approve(tid, vno)
            self._version = (tid, vno, "APPROVED")
            self.approve.setText("Одобрен ✓")
            self.approve.setEnabled(False)
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, human_error(str(exc)))

    def _tick(self):
        n = max(1, self.core.length())
        self.wave.pos = min(1.0, self.core.pos / n)
        self.wave.update()
        s = int(self.core.pos / self.core.rate)
        self.time.setText(f"{s // 60}:{s % 60:02d}")
        self.play.setText("Пауза" if self.core.playing else "▶  Слушать")

    def keyPressEvent(self, ev):  # noqa: N802
        if ev.key() == Qt.Key_Space:
            self.toggle()
        elif ev.key() == Qt.Key_1:
            self.switch("src")
        elif ev.key() == Qt.Key_2:
            self.switch("master")
        else:
            super().keyPressEvent(ev)

    def done(self, code):
        self.core.playing = False
        if self.sink is not None:
            self.sink.stop()
        super().done(code)


class FlowWindow(QMainWindow):
    def __init__(self, folder: str | Path | None = None, *, base_dir: str | Path | None = None, autoload: bool = True):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setMinimumSize(1180, 760)
        self.resize(1480, 940)
        self.setStyleSheet(T.LUX_STYLE)
        self.base = Path(base_dir) if base_dir else data_dir()
        self.base.mkdir(parents=True, exist_ok=True)
        self.engine = None
        self.folder: str | None = None
        self.album_filter: Path | None = None
        self.album_id: int | None = None
        self.status_data: dict | None = None
        self._task: Task | None = None
        self._thread: QThread | None = None
        self._kind = ""
        self._threads: list = []
        self._children: list = []
        self._closing = False
        self._build()
        self._set_running(False)
        self.check_ffmpeg()
        last = folder or (self._state().get("last_folder") if autoload else None)
        if last and Path(last).is_dir():
            self.open_folder(str(last))

    # ================================================================== persistence
    def _state(self) -> dict:
        try:
            return json.loads((self.base / "flow.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, **update):
        st = self._state()
        st.update(update)
        (self.base / "flow.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")

    # ================================================================== layout
    def _build(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        top = QFrame()
        top.setObjectName("TopBar")
        top.setFixedHeight(64)
        bar = QHBoxLayout(top)
        bar.setContentsMargins(24, 0, 20, 0)
        brand = QLabel("MUSIC RELEASE MASTER")
        brand.setObjectName("Brand")
        bar.addWidget(brand)
        bar.addSpacing(22)
        self.folder_label = QLabel("Папка не выбрана")
        self.folder_label.setObjectName("Muted")
        bar.addWidget(self.folder_label, 1)
        self.btn_open = self._btn("Открыть папку…", self.pick_folder, "Gold",
                                  "Папка со всеми альбомами — откроются все. Папка одного альбома — откроется он один.")
        self.btn_rescan = self._btn("Обновить папку", self.rescan, None, "Перечитать папку: новые и изменённые файлы.")
        self.btn_versus = self._btn("A/B сравнение версий", self.open_versus, None,
                                    "Слепое сравнение нескольких версий одной песни на слух — 2, 3, 10 файлов.")
        self.btn_expert = self._btn("Экспертный режим", self.open_expert, "Ghost",
                                    "Подробные измерения, ручные настройки обработки, метаданные.")
        for b in (self.btn_open, self.btn_rescan, self.btn_versus, self.btn_expert):
            bar.addWidget(b)
        outer.addWidget(top)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(300)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 18, 12, 14)
        sl.setSpacing(10)
        self.albums_title = QLabel("АЛЬБОМЫ")
        self.albums_title.setObjectName("Kicker")
        sl.addWidget(self.albums_title)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск альбома…")
        self.search.textChanged.connect(self._filter_albums)
        sl.addWidget(self.search)
        self.album_list = QListWidget()
        self.album_list.currentItemChanged.connect(lambda cur, prev: self._album_selected(cur))
        sl.addWidget(self.album_list, 1)
        self.btn_auto_all = self._btn("АВТО для всех альбомов", self.auto_all, None,
                                      "Проходит все этапы для каждого альбома по очереди. Это может занять часы.")
        sl.addWidget(self.btn_auto_all)
        body.addWidget(side)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._welcome_page())
        self.pages.addWidget(self._album_page())
        body.addWidget(self.pages, 1)
        wrap = QWidget()
        wrap.setLayout(body)
        outer.addWidget(wrap, 1)
        self.setCentralWidget(root)

    def _btn(self, text, slot, kind=None, tip=None) -> QPushButton:
        b = QPushButton(text)
        if kind:
            b.setObjectName(kind)
        if tip:
            b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda checked=False: slot())
        return b

    def _welcome_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(60, 40, 60, 40)
        lay.addStretch()
        hero = QFrame()
        hero.setObjectName("Hero")
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(44, 38, 44, 38)
        hl.setSpacing(14)
        k = QLabel("ОТ ПАПКИ С МУЗЫКОЙ — К ГОТОВОМУ РЕЛИЗУ")
        k.setObjectName("Kicker")
        t = QLabel("Откройте папку с альбомами")
        t.setObjectName("Title")
        d = QLabel("Выберите папку, где лежат все альбомы, — они появятся слева. Или папку одного альбома.\n"
                   "Дальше — шесть шагов по порядку или одна кнопка АВТО. Исходные файлы не изменяются.")
        d.setObjectName("Muted")
        d.setWordWrap(True)
        hl.addWidget(k)
        hl.addWidget(t)
        hl.addWidget(d)
        row = QHBoxLayout()
        big = self._btn("Открыть папку…", self.pick_folder, "Auto")
        row.addWidget(big)
        row.addStretch()
        hl.addLayout(row)
        self.welcome_note = QLabel("")
        self.welcome_note.setObjectName("Warn")
        self.welcome_note.setWordWrap(True)
        hl.addWidget(self.welcome_note)
        lay.addWidget(hero)
        lay.addStretch()
        return page

    def _album_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(26, 20, 26, 14)
        lay.setSpacing(14)

        head = QHBoxLayout()
        self.cover = QLabel()
        self.cover.setFixedSize(84, 84)
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setStyleSheet(f"background:{T.CARD}; border:1px solid {T.LINE}; border-radius:12px; color:{T.FAINT};")
        head.addWidget(self.cover)
        col = QVBoxLayout()
        col.setSpacing(4)
        self.album_title = QLabel("")
        self.album_title.setObjectName("AlbumTitle")
        self.album_meta = QLabel("")
        self.album_meta.setObjectName("Muted")
        col.addWidget(self.album_title)
        col.addWidget(self.album_meta)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Стиль альбома:"))
        self.style_combo = QComboBox()
        self.style_combo.setMinimumWidth(230)
        self.style_combo.addItem("Авто", None)
        for key in PRESETS:
            self.style_combo.addItem(STYLE_LABELS.get(key, key), key)
        self.style_combo.setToolTip("От стиля зависит, насколько громким и плотным будет мастер.\n"
                                    "«Авто» берёт стиль из строки «СТИЛЬ: …» в текстовом файле альбома или из "
                                    "названия альбома; если стиль не указан — бережный универсальный.")
        self.style_combo.activated.connect(lambda _: self.change_style())
        srow.addWidget(self.style_combo)
        self.style_note = QLabel("")
        self.style_note.setObjectName("Faint")
        srow.addWidget(self.style_note, 1)
        col.addLayout(srow)
        head.addLayout(col, 1)
        lay.addLayout(head)

        auto = QHBoxLayout()
        self.btn_auto = self._btn("⚡  АВТО — подготовить альбом к релизу", self.auto_album, "Auto",
                                  "Анализ → лучшие версии → ремонт → мастеринг → сборка релиза. Одной кнопкой.")
        auto.addWidget(self.btn_auto)
        cap = QLabel("Все шаги подряд, без вопросов.\nУже сделанное не повторяется.")
        cap.setObjectName("Muted")
        auto.addWidget(cap)
        auto.addStretch()
        self.chk_force = QCheckBox("Переделать заново")
        self.chk_force.setToolTip("Выполнить шаг ещё раз для всех треков, даже если он уже сделан\n"
                                  "(например, после смены стиля альбома).")
        auto.addWidget(self.chk_force)
        self.btn_stop = self._btn("Остановить", self.stop, "Danger", "Текущий трек будет доделан, затем работа остановится.")
        auto.addWidget(self.btn_stop)
        lay.addLayout(auto)

        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.cards: dict[str, StageCard] = {}
        for i, (key, title, hint, action, tip) in enumerate(STAGE_UI, start=1):
            c = StageCard(i, key, title, hint, action, tip)
            c.button.clicked.connect(lambda checked=False, k=key: self.run_stage(k))
            self.cards[key] = c
            cards.addWidget(c, 1)
        self.cards["versions"].extra.setText("Сравнить на слух (A/B)…")
        self.cards["versions"].extra.setToolTip("Открывает слепое сравнение версий в папке этого альбома.")
        self.cards["versions"].extra.clicked.connect(lambda checked=False: self.open_versus(album=True))
        self.cards["versions"].extra.show()
        self.cards["release"].extra.setText("Открыть папку релиза")
        self.cards["release"].extra.clicked.connect(lambda checked=False: self.open_release())
        lay.addLayout(cards)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["№", "Название", "Источник", "Стиль", "Анализ", "Ремонт", "Мастер",
                                              "Состояние"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(40)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._track_menu)
        self.table.doubleClicked.connect(lambda _: self.run_stage("listen"))
        self.table.setToolTip("Двойной щелчок — прослушать трек. Правая кнопка — исключить из релиза или вернуть.")
        lay.addWidget(self.table, 1)

        prow = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress_label = QLabel("Готов к работе")
        self.progress_label.setObjectName("Muted")
        prow.addWidget(self.progress_label, 1)
        self.btn_ready = self._btn("Я прослушал альбом — он готов", self.approve_album, None,
                                   "Отмечает все мастера как прослушанные и одобренные и собирает финальный пакет.")
        prow.addWidget(self.btn_ready)
        lay.addWidget(self.progress)
        lay.addLayout(prow)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(128)
        self.log.setPlaceholderText("Здесь появится понятный отчёт о каждом шаге.")
        lay.addWidget(self.log)
        return page

    # ================================================================== small helpers
    def say(self, text: str, color: str | None = None):
        body = html.escape(text)
        lead = len(body) - len(body.lstrip(" "))
        self.log.appendHtml(f'<span style="color:{color or T.TEXT}">{"&nbsp;" * lead}{body.lstrip(" ")}</span>')

    def check_ffmpeg(self) -> bool:
        get_tools.cache_clear()
        ok = tools_status()["status"] == "AVAILABLE"
        self.welcome_note.setText("" if ok else
                                  f"Не найден FFmpeg — без него программа не читает аудио. Установите: {FFMPEG_CMD}\n"
                                  "или положите ffmpeg.exe и ffprobe.exe в папку tools\\ffmpeg рядом с программой.")
        return ok

    def _set_running(self, running: bool, text: str = ""):
        self._running = running
        has_album = self.album_id is not None
        for b in (self.btn_open, self.btn_rescan, self.btn_auto_all, self.style_combo, self.chk_force, self.btn_ready):
            b.setEnabled(not running)
        self.btn_rescan.setEnabled(not running and self.engine is not None)
        self.btn_auto_all.setEnabled(not running and self.album_list.count() > 0)
        self.btn_auto.setEnabled(not running and has_album)
        for c in self.cards.values():
            c.button.setEnabled(not running and has_album)
        self.btn_stop.setVisible(running)
        self.album_list.setEnabled(not running)
        if text:
            self.progress_label.setText(text)
        if not running:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)

    # ================================================================== folder
    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Папка с альбомами или папка одного альбома", self.folder or "")
        if d:
            self.open_folder(d)

    def _project_for(self, folder: Path) -> tuple[Path, Path, Path | None]:
        """(project dir, library root, album filter). A folder inside an already opened library reuses that
        library's project, so work done there is kept, and shows only the albums below the folder."""
        roots = self._state().get("roots", {})
        for root, project in roots.items():
            try:
                rel = folder.resolve().relative_to(Path(root).resolve())
            except ValueError:
                continue
            if Path(project).is_dir() and rel.parts and not any(p.startswith("_") for p in rel.parts):
                return Path(project), Path(root), folder.resolve()
        key = hashlib.sha1(os.path.normcase(str(folder.resolve())).encode("utf-8")).hexdigest()[:8]
        return self.base / "Projects" / f"{safe_name(folder.name or 'music', 40)}-{key}", folder.resolve(), None

    def open_folder(self, folder: str):
        from srm.service.engine import Engine

        if self._running:
            return
        if not self.check_ffmpeg():
            QMessageBox.warning(self, APP_NAME, self.welcome_note.text())
            return
        path = Path(folder)
        project, root, album_filter = self._project_for(path)
        self.engine = Engine(project)
        self.engine.name_root_album_by_folder = True
        self.folder, self.album_filter = str(path), album_filter
        self.album_id = None
        roots = self._state().get("roots", {})
        roots[str(root)] = str(project)
        self._save_state(last_folder=str(path), roots=roots)
        self.folder_label.setText(str(path))
        self.pages.setCurrentIndex(1)
        self.log.clear()
        self._scan(root)

    def rescan(self):
        if self.engine and self.folder:
            root = next((Path(r) for r, p in self._state().get("roots", {}).items()
                         if Path(p) == self.engine.root), Path(self.folder))
            self._scan(root)

    def _scan(self, root: Path):
        engine = self.engine
        rid = engine.add_library_root(root, exclude=SERVICE_DIRS)

        def work(task: Task):
            return engine.scan(rid, cancel=task.cancel,
                               progress=lambda i, n, p: task.progress.emit("scan", i + 1, n, Path(p).name))

        self._start(work, "scan", "Читаем папку…")

    # ================================================================== albums
    def refresh_albums(self, keep: int | None = None):
        rows = self.engine.albums_overview() if self.engine else []
        if self.album_filter is not None:
            ids = {r["id"] for r in self.engine.db.query("SELECT id, folder_hint FROM albums")
                   if r["folder_hint"] and self._under(Path(r["folder_hint"]), self.album_filter)}
            rows = [r for r in rows if r["id"] in ids]
        self.album_list.blockSignals(True)
        self.album_list.clear()
        target = None
        for r in rows:
            state = ("релиз собран" if r["released"] else "мастеринг готов" if r["mastered"] >= r["tracks"] else
                     f"в работе: {r['mastered']} из {r['tracks']}" if r["mastered"] else "новый")
            it = QListWidgetItem(f"{r['title']}\n{r['tracks']} треков · {state}")
            it.setData(Qt.UserRole, r["id"])
            it.setToolTip(r["name"])
            if r["released"]:
                it.setForeground(QColor(T.GOLD_HI))
            self.album_list.addItem(it)
            if target is None or r["id"] == keep:
                target = it
        self.album_list.blockSignals(False)
        self.albums_title.setText(f"АЛЬБОМЫ · {len(rows)}")
        self._filter_albums(self.search.text())
        if target is not None:
            self.album_list.setCurrentItem(target)
            self._album_selected(target)
        else:
            self.album_id = None
            self.album_title.setText("В папке не найдено музыки")
            self.album_meta.setText("Поддерживаются WAV, MP3, FLAC, M4A, AAC, OGG. Папки, начинающиеся с «_», пропускаются.")
            self.table.setRowCount(0)
        self._set_running(self._running)

    @staticmethod
    def _under(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent)
            return True
        except ValueError:
            return False

    def _filter_albums(self, text: str):
        needle = text.casefold().strip()
        for i in range(self.album_list.count()):
            it = self.album_list.item(i)
            it.setHidden(bool(needle) and needle not in it.text().casefold())

    def _album_selected(self, item):
        if item is None or self.engine is None:
            return
        self.album_id = item.data(Qt.UserRole)
        self.refresh_album()
        self._set_running(self._running)

    def refresh_album(self):
        if self.engine is None or self.album_id is None:
            return
        st = self.status_data = self.engine.pipeline_status(self.album_id)
        al, rows = st["album"], st["tracks"]
        self.album_title.setText(al["title"])
        lossy = sum(r["is_lossy"] for r in rows)
        total_s = sum(r["duration_s"] or 0 for r in rows)
        src = ("все источники сжатые (качество исходника ограничено)" if lossy == len(rows) and rows else
               f"сжатых источников: {lossy} из {len(rows)}" if lossy else "источники без потерь")
        self.album_meta.setText(f"{len(rows)} треков · {int(total_s // 60)} мин · {src}")
        self.style_combo.blockSignals(True)
        manual = al["style"]["preset"] if al["style"]["source"] == "выбран вами" else None
        self.style_combo.setCurrentIndex(max(0, self.style_combo.findData(manual)))
        self.style_combo.blockSignals(False)
        label = STYLE_LABELS.get(al["style"]["preset"] or "generic")
        self.style_note.setText(f"сейчас: {label} — {al['style']['source']}")
        self._load_cover()
        self.table.setRowCount(len(rows))
        for r, t in enumerate(rows):
            if t["bits"]:
                quality = f"{t['bits']} бит"
            elif t["bit_rate"]:
                quality = f"{t['bit_rate'] // 1000} кбит/с"
            else:
                quality = ""
            source = " · ".join(filter(None, [Path(t["file"]).suffix.lstrip(".").upper(), quality,
                                              "сжатый" if t["is_lossy"] else "без потерь"]))
            rep = t["repair"]
            if t["excluded"]:
                state, color = "не идёт в релиз: " + (t["excluded_reason"] or ""), T.FAINT
            elif t["missing"]:
                state, color = "файл не найден на диске", T.BAD
            elif t["approved"]:
                state, color = "готов: прослушан и одобрен", T.GOOD
            elif t["mastered"]:
                state, color = "мастер готов — можно прослушать", T.GOLD_HI
            elif rep is not None:
                state, color = "отремонтирован — ждёт мастеринга", T.TEXT
            elif t["analyzed"]:
                state, color = "проверен", T.TEXT
            else:
                state, color = "новый", T.MUTED
            cells = [(t["track_no"] or "", None), (t["title"], None), (source, T.WARN if t["is_lossy"] else T.MUTED),
                     (t["style_label"], None if t["style"] else T.MUTED),
                     ("✓" if t["analyzed"] else "—", T.STAGE_COLORS["analyze"] if t["analyzed"] else T.FAINT),
                     ("—" if rep is None else ("✓ " + str(rep["applied"]) if rep["applied"] else "✓ чисто"),
                      T.STAGE_COLORS["repair"] if rep is not None else T.FAINT),
                     ("✓" if t["mastered"] else "—", T.STAGE_COLORS["master"] if t["mastered"] else T.FAINT),
                     (state, color)]
            for c, (val, col) in enumerate(cells):
                it = QTableWidgetItem(str(val))
                it.setData(Qt.UserRole, t["id"])
                if c in (0, 4, 5, 6):
                    it.setTextAlignment(Qt.AlignCenter)
                if t["excluded"]:
                    it.setForeground(QColor(T.FAINT))
                elif col:
                    it.setForeground(QColor(col))
                if c == 3:
                    it.setToolTip(f"Стиль: {t['style_source']}")
                if c == 5 and rep is not None:
                    it.setToolTip(rep["summary"])
                if c == 7:
                    it.setToolTip(state)
                self.table.setItem(r, c, it)
        sg = st["stages"]
        active = [t for t in rows if not t["excluded"]]

        def frac(key, word="готово"):
            d, n = sg[key]["done"], sg[key]["total"]
            return (f"{word}: {d} из {n}" if n else "нечего делать"), bool(n and d >= n)

        self.cards["analyze"].set_state(*frac("analyze", "проверено"))
        if sg["versions"]["total"]:
            self.cards["versions"].set_state(*frac("versions", "выбрано"))
        else:
            self.cards["versions"].set_state("повторных версий нет", True)
        self.cards["repair"].set_state(*frac("repair"))
        self.cards["master"].set_state(*frac("master"))
        approved = sum(t["approved"] for t in active)
        self.cards["listen"].set_state(f"одобрено: {approved} из {len(active)}", bool(active and approved == len(active)))
        exp = al["export"]
        draft = bool(exp) and exp["status"] != "TECHNICAL_PASS_AND_HUMAN_GATES_RECORDED"
        self.cards["release"].set_state(("не собран" if not sg["release"]["done"] else
                                         "черновик собран" if draft else "собран"),
                                        bool(sg["release"]["done"]))
        self.cards["release"].extra.setVisible(bool(sg["release"]["done"]))
        self.btn_ready.setVisible(bool(active) and all(t["mastered"] for t in active) and approved < len(active))

    def _load_cover(self):
        self.cover.setPixmap(QPixmap())
        self.cover.setText("нет\nобложки")
        try:
            cover = self.engine.album_artwork(self.album_id, verify=False)["cover"]
            path = cover["path"] if cover else self.engine.find_folder_cover(self.album_id)
            if path and Path(path).is_file():
                pm = QPixmap(str(path))
                if not pm.isNull():
                    self.cover.setPixmap(pm.scaled(82, 82, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
        except Exception:
            pass

    def change_style(self):
        if self.engine is None or self.album_id is None:
            return
        self.engine.set_album_style(self.album_id, self.style_combo.currentData())
        self.refresh_album()
        if any(t["mastered"] for t in self.status_data["tracks"]):
            self.say("Стиль изменён. Чтобы он повлиял на уже готовые мастера, отметьте «Переделать заново» и "
                     "запустите «Мастеринг».", T.WARN)

    def _track_menu(self, pos):
        it = self.table.itemAt(pos)
        if it is None or self._running or self.status_data is None:
            return
        tid = it.data(Qt.UserRole)
        t = next(x for x in self.status_data["tracks"] if x["id"] == tid)
        menu = QMenu(self)
        if t["excluded"]:
            menu.addAction("Вернуть в релиз", lambda: self._exclude(tid, False))
        else:
            menu.addAction("Не включать в релиз", lambda: self._exclude(tid, True))
        if t["mastered"]:
            menu.addAction("Прослушать: оригинал и мастер", lambda: self.listen(tid))
        menu.addAction("Показать файл в папке", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(Path(self.engine.track(tid)["path"]).parent))))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _exclude(self, tid: int, excluded: bool):
        self.engine.set_track_excluded(tid, excluded)
        self.refresh_album()

    # ================================================================== running work
    def _start(self, fn, kind: str, text: str):
        if self._running:
            return
        self._kind = kind
        self._task = Task(fn)
        self._thread = QThread(self)
        self._task.moveToThread(self._thread)
        self._thread.started.connect(self._task.run)
        self._task.progress.connect(self._on_progress)
        self._task.stage_done.connect(self._on_stage)
        self._task.finished.connect(self._on_done)
        self._task.failed.connect(self._on_fail)
        self._task.finished.connect(self._thread.quit)
        self._task.failed.connect(self._thread.quit)
        self._set_running(True, text)
        self.progress.setRange(0, 0)
        self._thread.start()

    def stop(self):
        if self._task is not None:
            self._task.cancel.set()
            self.progress_label.setText("Останавливаем: доделываем текущий трек…")

    @Slot(str, int, int, str)
    def _on_progress(self, stage: str, done: int, total: int, text: str):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        name = {"scan": "Читаем файлы", "album": "Альбом"}.get(stage, STAGE_NAMES.get(stage, stage))
        self.progress_label.setText(f"{name}: {done} из {total}" + (f"  ·  {text}" if text else ""))

    @Slot(dict)
    def _on_stage(self, res: dict):
        self._log_stage(res)
        self.refresh_album()

    def _log_stage(self, res: dict):
        color = T.STAGE_COLORS.get(res["stage"], T.TEXT)
        head = (f"[{res['album']}] " if res.get("album") else "") + res["name"]
        bits = []
        if res["done"]:
            bits.append(f"сделано {res['done']}")
        if res["skipped"]:
            bits.append(f"уже было готово {res['skipped']}")
        if res["failed"]:
            bits.append(f"ошибок {len(res['failed'])}")
        if res.get("cancelled"):
            bits.append("остановлено")
        self.say(f"● {head}: " + (", ".join(bits) or "без изменений"), color)
        for m in res["messages"][:40]:
            self.say("     " + m, T.MUTED)
        for f in res["failed"]:
            self.say(f"     ✗ {f['title']}: {human_error(f['error'])}", T.BAD)

    @Slot(object)
    def _on_done(self, result):
        kind = self._kind
        self._finish_thread()
        if kind == "scan":
            n = result.get("audio_files", 0)
            self.say(f"Папка прочитана: файлов {n}, альбомов {len(self.engine.albums_overview())}."
                     + (f" Не удалось прочитать: {len(result['errors'])}." if result.get("errors") else ""), T.GOLD_HI)
            self.refresh_albums(self.album_id)
            self._set_running(False, "Готов к работе")
            return
        if kind == "stage":
            self._log_stage(result)
        elif kind == "auto":
            ok = bool(result) and result[-1]["stage"] == "release" and result[-1].get("done") and not any(
                r["failed"] for r in result)
            self.say("АВТО завершено: пакет релиза собран. Осталось прослушать альбом." if ok else
                     "АВТО завершено не полностью — смотрите сообщения выше.", T.GOOD if ok else T.WARN)
        elif kind == "all":
            self.say(f"АВТО для всех альбомов: обработано {result}.", T.GOLD_HI)
        keep = self.album_id
        self.refresh_albums(keep)
        self._set_running(False, "Готово")

    @Slot(str)
    def _on_fail(self, message: str):
        self._finish_thread()
        self.say("✗ " + human_error(message), T.BAD)
        if self.engine is not None:
            self.refresh_albums(self.album_id)
        self._set_running(False, "Остановлено из-за ошибки")

    def _finish_thread(self):
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
        self._thread, self._task = None, None
        if self._closing:
            QTimer.singleShot(0, self.close)

    def run_stage(self, key: str):
        if self.engine is None or self.album_id is None or self._running:
            return
        if key == "listen":
            self.listen()
            return
        engine, aid, force = self.engine, self.album_id, self.chk_force.isChecked()
        self._start(lambda task: engine.run_stage(aid, key, progress=task.progress.emit, cancel=task.cancel,
                                                  force=force), "stage", f"{STAGE_NAMES[key]}…")

    def auto_album(self):
        if self.engine is None or self.album_id is None or self._running:
            return
        engine, aid = self.engine, self.album_id
        self.say("АВТО: " + " → ".join(n for _, n in STAGES), T.GOLD_HI)
        self._start(lambda task: engine.run_auto(aid, progress=task.progress.emit, cancel=task.cancel,
                                                 on_stage=task.stage_done.emit), "auto", "АВТО…")

    def auto_all(self):
        if self.engine is None or self._running:
            return
        albums = [(self.album_list.item(i).data(Qt.UserRole), self.album_list.item(i).text().split("\n")[0])
                  for i in range(self.album_list.count())]
        if not albums or QMessageBox.question(
                self, APP_NAME, f"Пройти все этапы для {len(albums)} альбомов по очереди?\n\n"
                "Это может занять несколько часов. Работу можно остановить в любой момент — "
                "сделанное сохранится, и в следующий раз продолжится с того же места.") != QMessageBox.Yes:
            return
        engine = self.engine

        def work(task: Task):
            n = 0
            for aid, title in albums:
                if task.cancel.is_set():
                    break
                engine.run_auto(aid, progress=task.progress.emit, cancel=task.cancel,
                                on_stage=lambda r, t=title: task.stage_done.emit({**r, "album": t}))
                n += 1
            return f"{n} из {len(albums)}"

        self.say(f"АВТО для всех альбомов: {len(albums)}.", T.GOLD_HI)
        self._start(work, "all", "АВТО для всех альбомов…")

    # ================================================================== listening / release / other windows
    def listen(self, track_id: int | None = None):
        if self.status_data is None:
            return
        ids = [t["id"] for t in self.status_data["tracks"] if t["mastered"] and not t["excluded"]]
        if not ids:
            QMessageBox.information(self, APP_NAME, "Пока нечего слушать: сначала выполните шаг «Мастеринг» "
                                                    "или нажмите «АВТО».")
            return
        if track_id is None:
            sel = self.table.selectedItems()
            track_id = sel[0].data(Qt.UserRole) if sel else ids[0]
        if track_id not in ids:
            track_id = ids[0]
        ListenDialog(self, self.engine, ids, track_id).exec()
        self.refresh_album()

    def approve_album(self):
        if self.engine is None or self.album_id is None or self._running:
            return
        if QMessageBox.question(
                self, APP_NAME, "Подтверждаете, что прослушали мастера этого альбома и они вас устраивают?\n\n"
                "Все треки будут отмечены как одобренные, и будет собран финальный пакет.") != QMessageBox.Yes:
            return
        n = self.engine.approve_album(self.album_id)
        self.say(f"Одобрено треков: {n}. Собираем финальный пакет…", T.GOOD)
        self.run_stage("release")

    def open_release(self):
        exp = (self.status_data or {}).get("album", {}).get("export")
        if exp and Path(exp["path"]).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(exp["path"]))

    def open_versus(self, album: bool = False):
        from srm.versus.gui import VersusWindow

        folder = None
        if album and self.status_data and self.status_data["album"]["folder"]:
            folder = self.status_data["album"]["folder"]
        w = VersusWindow(folder=folder or self.folder)
        self._children.append(w)
        w.show()

    def open_expert(self):
        if self.engine is None:
            QMessageBox.information(self, APP_NAME, "Сначала откройте папку с музыкой.")
            return
        from srm.gui.studio import StudioWindow

        w = StudioWindow(str(self.engine.root))
        self._children.append(w)
        w.show()

    def closeEvent(self, event):  # noqa: N802
        if self._running and self._thread is not None:
            event.ignore()
            if self._closing:
                return
            if QMessageBox.question(self, APP_NAME, "Обработка ещё идёт. Остановить после текущего трека и закрыть "
                                                    "программу?") != QMessageBox.Yes:
                return
            self._closing = True
            self.stop()
            return
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyleSheet(T.LUX_STYLE)
    w = FlowWindow(argv[0] if argv else None)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
