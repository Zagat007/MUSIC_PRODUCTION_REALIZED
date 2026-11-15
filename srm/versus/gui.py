"""Desktop interface of the blind version test (Russian). Thin layer over ``srm.versus.core``.

During a test only the letters A, B, C, ... are shown. Real file names appear on the technical
screen (before the letters are known) and again only after the test is finished.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from srm import __version__
from srm.core.tools import get_tools, tools_status
from srm.gui.app import _thread_running, run_bg
from srm.gui.studio import STUDIO_STYLE, button, label
from srm.versus import core as C
from srm.versus.player import RATE, MixerCore, make_device
from srm.versus.store import Store

APP_TITLE = "Сравнение версий"
PAGES = ["Папка", "Песни", "Проверка", "Прослушивание", "Таблица", "Итог", "Применение", "История"]
FFMPEG_CMD = "winget install Gyan.FFmpeg"


def _mmss(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _item(text, align=None) -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    if align:
        it.setTextAlignment(align)
    return it


class WaveView(QWidget):
    """Waveform of the audible version with playhead, chorus mark and loop region. Click to seek."""

    seek = Signal(float)  # fraction 0..1

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(120)
        self.peaks: list[float] = []
        self.pos = 0.0
        self.chorus: float | None = None
        self.loop: tuple[float, float] | None = None

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#191e1f"))
        if self.loop:
            p.fillRect(int(self.loop[0] * w), 0, max(2, int((self.loop[1] - self.loop[0]) * w)), h,
                       QColor(217, 185, 151, 45))
        if self.peaks:
            p.setPen(QPen(QColor("#b8cbb7"), 1))
            n = len(self.peaks)
            for i, v in enumerate(self.peaks):
                x = int(i * w / n)
                y = int((1 - min(1.0, v)) * h / 2)
                p.drawLine(x, y, x, h - y)
        if self.chorus is not None:
            p.setPen(QPen(QColor("#d9b997"), 2))
            p.drawLine(int(self.chorus * w), 0, int(self.chorus * w), h)
        p.setPen(QPen(QColor("#ffffff"), 1))
        p.drawLine(int(self.pos * w), 0, int(self.pos * w), h)

    def mousePressEvent(self, ev):  # noqa: N802
        self.seek.emit(max(0.0, min(1.0, ev.position().x() / max(1, self.width()))))


class WeightsDialog(QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Веса критериев")
        form = QFormLayout(self)
        form.addRow(label("Чем больше вес, тем сильнее критерий влияет на итог. 0 — не учитывать.", "Muted"))
        self.spins = {}
        for key, name in C.CRITERIA:
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 5.0)
            sp.setSingleStep(0.5)
            sp.setValue(float(settings["weights"].get(key, 1.0)))
            self.spins[key] = sp
            form.addRow(name, sp)
        self.share = QSpinBox()
        self.share.setRange(0, 100)
        self.share.setSuffix(" %")
        self.share.setValue(int(round(100 * settings["pair_share"])))
        form.addRow("Доля попарных сравнений в итоге", self.share)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)

    def result_settings(self, settings: dict) -> dict:
        out = dict(settings)
        out["weights"] = {k: sp.value() for k, sp in self.spins.items()}
        out["pair_share"] = self.share.value() / 100.0
        return out


class VersusWindow(QMainWindow):
    def __init__(self, store_path: str | Path | None = None, folder: str | Path | None = None, audio: bool = True):
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE} · Suno Release Master {__version__}")
        self.setMinimumSize(1100, 720)
        self.resize(1360, 880)
        self.setStyleSheet(STUDIO_STYLE)
        if store_path is None:
            # One shared location whether started alone or from the main program (independent of the app name).
            base = Path(QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation) or Path.home())
            store_path = base / "SunoReleaseMaster" / "versus.sqlite"
        self.store = Store(store_path)
        self.settings = self.store.settings()
        self._threads: list = []
        self.root: str | None = None
        self.groups: list[dict] = []
        self.session_id: int | None = None
        self.state: dict | None = None
        self.result: dict | None = None
        self.core = MixerCore()
        self.sink = None
        self._audio_wanted = audio
        self._loaded_for: int | None = None
        self._shown: list[str] = []
        self._pair: dict | None = None
        self._loop_start: int | None = None
        self._busy = False
        self._build()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(60)
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self.toggle_play)
        for i in range(9):
            QShortcut(QKeySequence(str(i + 1)), self, activated=lambda n=i: self._switch_index(n))
        self.refresh_ffmpeg()
        last = folder or self.store.get_value("last_folder")
        if last and Path(last).is_dir():
            self.open_folder(str(last))
        else:
            self.go(0)

    # ================================================================== layout
    def _build(self):
        shell = QWidget()
        outer = QHBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        side_frame = QFrame()
        side_frame.setObjectName("Sidebar")
        side_frame.setFixedWidth(220)
        side = QVBoxLayout(side_frame)
        side.setContentsMargins(20, 30, 20, 22)
        side.setSpacing(6)
        side.addWidget(label("A / B", "Brand"))
        side.addWidget(label("СЛЕПОЕ СРАВНЕНИЕ ВЕРСИЙ", "Muted"))
        side.addSpacing(30)
        self.nav = []
        for i, name in enumerate(PAGES):
            b = button(f"{i + 1:02d}   {name}", lambda checked=False, n=i: self.go(n), "Nav")
            b.setCheckable(True)
            self.nav.append(b)
            side.addWidget(b)
        side.addStretch()
        side.addWidget(button("Веса критериев…", self.edit_weights, "Quiet"))
        side.addWidget(label("●  Оригиналы не изменяются", "Muted"))
        outer.addWidget(side_frame)
        main = QVBoxLayout()
        main.setContentsMargins(34, 26, 34, 14)
        main.setSpacing(14)
        self.title = label("", "Title")
        self.subtitle = label("", "Muted")
        main.addWidget(self.title)
        main.addWidget(self.subtitle)
        self.pages = QStackedWidget()
        for build in (self._page_folder, self._page_groups, self._page_tech, self._page_listen, self._page_table,
                      self._page_result, self._page_apply, self._page_history):
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(14)
            build(lay)
            self.pages.addWidget(page)
        main.addWidget(self.pages, 1)
        outer.addLayout(main, 1)
        self.setCentralWidget(shell)
        self.status = self.statusBar()

    def _table(self, headers, stretch=0) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.verticalHeader().hide()
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        t.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.Stretch)
        return t

    def _page_folder(self, lay):
        lay.addWidget(label("Выберите папку, в которой лежат версии одной песни — или папку с несколькими песнями. "
                            "Файлы, названия которых отличаются только номером версии, объединяются в группу.", "Muted"))
        row = QHBoxLayout()
        row.addWidget(button("Выбрать папку…", self.pick_folder, "Primary"))
        self.last_button = button("", lambda: self.open_folder(self.store.get_value("last_folder")))
        self.last_button.hide()
        row.addWidget(self.last_button)
        row.addStretch()
        lay.addLayout(row)
        self.ffmpeg_label = label("", "Muted")
        lay.addWidget(self.ffmpeg_label)
        frow = QHBoxLayout()
        self.ffmpeg_copy = button("Скопировать команду установки", lambda: (
            QGuiApplication.clipboard().setText(FFMPEG_CMD), self.status.showMessage("Команда скопирована.")))
        self.ffmpeg_retry = button("Проверить снова", self.refresh_ffmpeg)
        frow.addWidget(self.ffmpeg_copy)
        frow.addWidget(self.ffmpeg_retry)
        frow.addStretch()
        lay.addLayout(frow)
        lay.addStretch()

    def _page_groups(self, lay):
        self.groups_table = self._table(["Песня", "Версий", "Статус", "Победитель", "Папка"], stretch=0)
        self.groups_table.doubleClicked.connect(lambda _: self.open_selected_group())
        lay.addWidget(self.groups_table, 1)
        row = QHBoxLayout()
        row.addWidget(button("Открыть песню", self.open_selected_group, "Primary"))
        row.addWidget(button("Начать тест заново", lambda: self.open_selected_group(restart=True)))
        row.addWidget(button("Обновить список", lambda: self.root and self.open_folder(self.root)))
        row.addStretch()
        lay.addLayout(row)

    def _page_tech(self, lay):
        self.tech_table = self._table(["", "Версия", "Файл", "Статус", "Длит.", "Формат", "Гц", "Качество", "LUFS",
                                       "Пик, dBTP", "PLR", "Оценка", "Замечания"], stretch=12)
        lay.addWidget(self.tech_table, 1)
        self.tech_summary = label("", "Muted")
        lay.addWidget(self.tech_summary)
        row = QHBoxLayout()
        row.addWidget(label("Режим:"))
        self.mode_combo = QComboBox()
        for key, name in C.MODES.items():
            self.mode_combo.addItem(name, key)
        self.mode_combo.activated.connect(lambda _: self.apply_mode())
        row.addWidget(self.mode_combo)
        self.mode_hint = label("", "Muted")
        row.addWidget(self.mode_hint, 1)
        self.start_button = button("Начать слепое прослушивание  →", self.start_listening, "Primary")
        row.addWidget(self.start_button)
        lay.addLayout(row)

    def _page_listen(self, lay):
        self.listen_head = label("", "TrackTitle")
        self.listen_note = label("", "Muted")
        lay.addWidget(self.listen_head)
        lay.addWidget(self.listen_note)
        self.version_row = QHBoxLayout()
        lay.addLayout(self.version_row)
        self.version_buttons: dict[str, object] = {}
        self.wave = WaveView()
        self.wave.seek.connect(self._seek_fraction)
        lay.addWidget(self.wave)
        row = QHBoxLayout()
        self.play_button = button("▶  Слушать", self.toggle_play, "Primary")
        row.addWidget(self.play_button)
        row.addWidget(button("Вступление", lambda: self._seek_seconds(0)))
        row.addWidget(button("Припев", self.jump_chorus))
        row.addWidget(button("Отметить припев здесь", self.mark_chorus, "Quiet"))
        row.addWidget(button("Финал", self.jump_ending))
        row.addStretch()
        self.time_label = label("0:00", "SectionTitle")
        self.time_label.setWordWrap(False)
        row.addWidget(self.time_label)
        lay.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(button("Начало петли", self.loop_start, "Quiet"))
        row.addWidget(button("Конец петли", self.loop_end, "Quiet"))
        self.loop_button = button("Петля", self.loop_toggle)
        self.loop_button.setCheckable(True)
        row.addWidget(self.loop_button)
        row.addStretch()
        vol = label("Громкость прослушивания")
        vol.setWordWrap(False)
        row.addWidget(vol)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(180)
        self.volume.valueChanged.connect(lambda v: setattr(self.core, "volume", v / 100.0))
        self.core.volume = 0.8
        row.addWidget(self.volume)
        lay.addLayout(row)
        self.rating_table = QTableWidget(len(C.CRITERIA), 1)
        self.rating_table.setVerticalHeaderLabels([name for _, name in C.CRITERIA])
        self.rating_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.rating_table.verticalHeader().setDefaultSectionSize(38)
        self.rating_table.setStyleSheet("QSpinBox { padding:2px 8px; min-height:24px; }")
        lay.addWidget(self.rating_table, 1)
        self.verdict_row = QHBoxLayout()
        lay.addLayout(self.verdict_row)

    def _page_table(self, lay):
        self.standings_table = self._table(["Место", "Вариант", "Файл", "Побед", "Ничьих", "Поражений",
                                            "Средняя оценка", "Штраф", "Итог"], stretch=2)
        lay.addWidget(self.standings_table, 2)
        self.pairs_table = self._table(["Этап", "Сравнение", "Результат"], stretch=2)
        lay.addWidget(self.pairs_table, 1)

    def _page_result(self, lay):
        self.result_head = label("", "TrackTitle")
        lay.addWidget(self.result_head)
        self.result_text = QPlainTextEdit()
        self.result_text.setReadOnly(True)
        lay.addWidget(self.result_text, 1)
        row = QHBoxLayout()
        self.final_button = button("Провести финальное A/B-сравнение", lambda: self.go(3), "Primary")
        row.addWidget(self.final_button)
        row.addWidget(label("Выбрать победителя вручную:"))
        self.override_combo = QComboBox()
        self.override_combo.setMinimumWidth(260)
        row.addWidget(self.override_combo)
        self.override_reason = QLineEdit()
        self.override_reason.setPlaceholderText("Почему (попадёт в отчёт)")
        row.addWidget(self.override_reason, 1)
        row.addWidget(button("Назначить", self.do_override))
        lay.addLayout(row)
        row2 = QHBoxLayout()
        self.confirm_button = button("Подтвердить победителя  →", lambda: self.go(6), "Primary")
        row2.addWidget(self.confirm_button)
        row2.addStretch()
        lay.addLayout(row2)

    def _page_apply(self, lay):
        self.apply_head = label("", "SectionTitle")
        lay.addWidget(self.apply_head)
        lay.addWidget(label("Ничего не перемещается и не переименовывается, пока вы не нажмёте «ПРИМЕНИТЬ РЕЗУЛЬТАТ». "
                            "Файлы никогда не удаляются.", "Muted"))
        r1 = QHBoxLayout()
        self.chk_rename = QCheckBox("Переименовать победителя:")
        self.spin_number = QSpinBox()
        self.spin_number.setRange(0, 99)
        self.spin_number.setSpecialValueText("без номера")
        self.edit_title = QLineEdit()
        r1.addWidget(self.chk_rename)
        r1.addWidget(self.spin_number)
        r1.addWidget(self.edit_title, 1)
        lay.addLayout(r1)
        r2 = QHBoxLayout()
        self.chk_album = QCheckBox("Переместить победителя в папку альбома:")
        self.album_path = QLineEdit()
        self.album_path.setReadOnly(True)
        r2.addWidget(self.chk_album)
        r2.addWidget(self.album_path, 1)
        r2.addWidget(button("Выбрать…", self.pick_album))
        lay.addLayout(r2)
        self.chk_defer = QCheckBox(f"Переместить остальные версии в папку «{C.DEFERRED_DIR}»")
        lay.addWidget(self.chk_defer)
        lay.addWidget(label("Если ничего не отмечено, файлы останутся на месте и будет сохранён только отчёт.", "Muted"))
        self.apply_preview = QPlainTextEdit()
        self.apply_preview.setReadOnly(True)
        lay.addWidget(self.apply_preview, 1)
        for w in (self.chk_rename, self.chk_album, self.chk_defer):
            w.toggled.connect(lambda _: self.refresh_apply())
        self.spin_number.valueChanged.connect(lambda _: self.refresh_apply())
        self.edit_title.textChanged.connect(lambda _: self.refresh_apply())
        row = QHBoxLayout()
        self.apply_button = button("ПРИМЕНИТЬ РЕЗУЛЬТАТ", self.do_apply, "Primary")
        row.addWidget(self.apply_button)
        row.addWidget(button("Отмена", lambda: self.go(5)))
        row.addStretch()
        lay.addLayout(row)

    def _page_history(self, lay):
        self.history_table = self._table(["Дата", "Песня", "Статус", "Победитель", "Уверенность", "Папка"], stretch=1)
        self.history_table.doubleClicked.connect(lambda _: self.open_history_row())
        lay.addWidget(self.history_table, 1)
        row = QHBoxLayout()
        row.addWidget(button("Открыть тест", self.open_history_row, "Primary"))
        row.addStretch()
        lay.addLayout(row)

    # ================================================================== navigation
    def go(self, index: int):
        if index in (2, 3, 4, 5, 6) and self.state is None:
            self.status.showMessage("Сначала выберите песню в списке.")
            index = 1 if self.groups else 0
        if index == 6 and not (self.result and self.result.get("winner")):
            self.status.showMessage("Победитель ещё не определён.")
            index = 5
        if index != 3:
            self.core.playing = False
        self.pages.setCurrentIndex(index)
        for i, b in enumerate(self.nav):
            b.setChecked(i == index)
        song = self.state["group"]["name"] if self.state else ""
        heads = [
            ("Выбор папки", "С чего начнём."),
            ("Песни", "Группы версий, найденные в папке, и их состояние."),
            (song or "Техническая проверка", "Измерения каждого файла. Оригиналы только читаются."),
            (song or "Слепое прослушивание", "Названия скрыты. Громкость версий выровнена — громче не значит лучше."),
            (song or "Турнирная таблица", "Ход сравнения."),
            (song or "Итог", "Результат и объяснение выбора."),
            (song or "Применение результата", "Что сделать с файлами."),
            ("История тестов", "Все проведённые сравнения."),
        ]
        self.title.setText(heads[index][0])
        self.subtitle.setText(heads[index][1])
        refresh = {1: self.refresh_groups, 2: self.refresh_tech, 3: self.refresh_listen, 4: self.refresh_table,
                   5: self.refresh_result, 6: self.refresh_apply, 7: self.refresh_history}.get(index)
        if refresh:
            refresh()

    def refresh_ffmpeg(self):
        get_tools.cache_clear()
        st = tools_status()
        ok = st["status"] == "AVAILABLE"
        self.ffmpeg_ok = ok
        self.ffmpeg_label.setText(
            f"FFmpeg найден: {st.get('version', '')}" if ok else
            "FFmpeg не найден — без него программа не может читать аудио.\n"
            f"Установите его командой:  {FFMPEG_CMD}\n"
            "или положите ffmpeg.exe и ffprobe.exe в папку tools\\ffmpeg рядом с программой, затем нажмите "
            "«Проверить снова».")
        self.ffmpeg_copy.setVisible(not ok)
        self.ffmpeg_retry.setVisible(not ok)
        last = self.store.get_value("last_folder")
        if last:
            self.last_button.setText(f"Открыть последнюю: {last}")
            self.last_button.show()

    # ================================================================== folder / groups
    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Папка с версиями песни", self.root or "")
        if d:
            self.open_folder(d)

    def open_folder(self, folder: str):
        try:
            self.groups = C.discover_groups(folder)
        except C.VersusError as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self.root = str(folder)
        self.store.set_value("last_folder", self.root)
        if not self.groups:
            self.status.showMessage("В папке не найдено песен с двумя и более версиями.")
        self.go(1)

    def refresh_groups(self):
        self.groups_table.setRowCount(len(self.groups))
        for r, g in enumerate(self.groups):
            s = self.store.active(g["key"])
            vals = [g["name"], len(g["files"]), s["status"] if s else C.NOT_STARTED,
                    (s or {}).get("winner") or "", g["folder"]]
            for c, v in enumerate(vals):
                self.groups_table.setItem(r, c, _item(v))
        if self.groups and not self.groups_table.selectedItems():
            self.groups_table.selectRow(0)

    def open_selected_group(self, restart: bool = False):
        rows = sorted({i.row() for i in self.groups_table.selectedIndexes()})
        if not rows or self._busy:
            return
        group = self.groups[rows[0]]
        existing = self.store.active(group["key"])
        if existing and not restart:
            self.load_session(existing["id"])
            return
        if restart and existing and QMessageBox.question(
                self, APP_TITLE, "Начать тест заново? Текущие оценки останутся в истории.") != QMessageBox.Yes:
            return
        if not self.ffmpeg_ok:
            QMessageBox.warning(self, APP_TITLE, self.ffmpeg_label.text())
            return
        self.analyze_group(group)

    def analyze_group(self, group: dict):
        self._busy = True
        self.status.showMessage(f"Проверяем {len(group['files'])} файлов: {group['name']}…")

        def work():
            out = []
            for f in group["files"]:
                try:
                    out.append(C.analyze_version(f))
                except Exception as exc:  # unreadable file: reported in the table, the rest continues
                    out.append({"path": f, "name": Path(f).name, "size": 0, "mtime": 0, "sha256": "",
                                "error": str(exc), "tech": {}, "waveform": []})
            return out

        def done(analyses):
            self._busy = False
            self.state = C.new_session(group, analyses, settings=self.settings)
            self.session_id = self.store.create(self.state)
            self._loaded_for = None
            self.save()
            self.status.showMessage("Проверка завершена.")
            self.go(2)

        def fail(message):
            self._busy = False
            QMessageBox.warning(self, APP_TITLE, "Не удалось проверить файлы:\n" + message)

        run_bg(self, work, on_done=done, on_fail=fail)

    def load_session(self, session_id: int):
        self.state = self.store.load(session_id)
        self.session_id = session_id
        self._loaded_for = None
        self.result = C.evaluate(self.state, self.settings)
        status = self.result["status"]
        self.go(5 if status in (C.WINNER, C.APPLIED, C.NEED_FINAL) else 2)

    def save(self):
        """Persist after every step, so the test can be resumed after closing the program."""
        if self.state is None or self.session_id is None:
            return
        self.result = C.evaluate(self.state, self.settings)
        winner = None
        if self.result["status"] in (C.WINNER, C.APPLIED) and self.result["winner"]:
            winner = C.version_of(self.state, self.result["winner"])["name"]
        self.store.save(self.session_id, self.state, status=self.result["status"], winner=winner,
                        confidence=self.result.get("confidence"))

    def edit_weights(self):
        dlg = WeightsDialog(self.settings, self)
        if dlg.exec() == QDialog.Accepted:
            self.settings = dlg.result_settings(self.settings)
            self.store.save_settings(self.settings)
            self.save()
            self.go(self.pages.currentIndex())

    # ================================================================== technical screen
    def _finished(self) -> bool:
        return bool(self.result and self.result["status"] in (C.WINNER, C.APPLIED))

    def refresh_tech(self):
        st = self.state
        self.tech_table.setRowCount(len(st["versions"]))
        selected_ids = {st["labels"][lab] for lab in (st["selected"] or [])}
        for r, v in enumerate(st["versions"]):
            t = v["tech"]
            chk = QTableWidgetItem()
            if v["status"] == C.OK:
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                chk.setCheckState(Qt.Checked if (not selected_ids or v["id"] in selected_ids) else Qt.Unchecked)
            else:
                chk.setFlags(Qt.NoItemFlags)
            self.tech_table.setItem(r, 0, chk)
            quality = (f"{t['bits_per_sample']} бит" if t.get("bits_per_sample") else
                       f"{t['bit_rate'] // 1000} кбит/с" if t.get("bit_rate") else "—")
            status = v["status"] + (f" версии {v['duplicate_of']}" if v["duplicate_of"] else "")
            notes = v["error"] or "; ".join(p["text"] for p in v["penalties"]) or "замечаний нет"
            vals = [f"Версия {v['id']}", v["name"], status, _mmss(t.get("duration_s")), (t.get("codec") or "—").upper(),
                    t.get("sample_rate") or "—", quality, t.get("integrated_lufs", "—"), t.get("true_peak_dbtp", "—"),
                    t.get("plr_db", "—"), v["tech_score"] if v["status"] == C.OK else "—", notes]
            for c, val in enumerate(vals, start=1):
                it = _item(val)
                if v["status"] != C.OK:
                    it.setForeground(QColor("#ffb36b"))
                self.tech_table.setItem(r, c, it)
        n = len(st["labels"])
        tw = ", ".join(f"Версия {i}" for i in C.technical_winner(st)) or "—"
        letters = "/".join(C.labels_for(n))
        self.tech_summary.setText(
            f"Допущено к сравнению: {n} из {len(st['versions'])}. Технически лучшие: {tw}. "
            + (f"Тест пройдёт в формате {letters}; буквы назначены случайно и здесь не показываются."
               if n >= 2 else "Для сравнения нужно не меньше двух допущенных версий."))
        started = any(p["result"] for p in st["pairs"]) or st["stage"] == "round" and st["mode"] == "tournament" \
            and bool(st.get("finalists"))
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(st["mode"]))
        self.mode_combo.setEnabled(not started and n >= 2)
        pairs = len([p for p in st["pairs"] if p["stage"] == "round"])
        self.mode_hint.setText({
            "round_robin": f"Каждая версия с каждой: сравнений — {pairs}.",
            "tournament": (f"Этап 1: оценка всех версий. Этап 2: круг из {C.FINALISTS} лучших."
                           if st["stage"] == "prelim" or st.get("finalists") else
                           f"Версий немного — пройдёт полный круг: сравнений — {pairs}."),
            "selected": f"Только отмеченные галочкой версии: сравнений — {pairs}.",
        }[st["mode"]])
        self.start_button.setEnabled(n >= 2)
        self.start_button.setText("Продолжить прослушивание  →" if started else "Начать слепое прослушивание  →")
        if n == 1:
            self.start_button.setEnabled(True)
            self.start_button.setText("Перейти к итогу  →")

    def _checked_labels(self) -> list[str]:
        st = self.state
        ids = {st["versions"][r]["id"] for r in range(self.tech_table.rowCount())
               if self.tech_table.item(r, 0).checkState() == Qt.Checked}
        return [lab for lab, vid in st["labels"].items() if vid in ids]

    def apply_mode(self) -> bool:
        mode = self.mode_combo.currentData()
        try:
            C.set_mode(self.state, mode, self._checked_labels() if mode == "selected" else None)
        except C.VersusError as exc:
            QMessageBox.information(self, APP_TITLE, str(exc))
            self.refresh_tech()
            return False
        self._loaded_for = None
        self.save()
        self.refresh_tech()
        return True

    def start_listening(self):
        st = self.state
        if len(st["labels"]) < 2:
            self.go(5)
            return
        if not any(p["result"] for p in st["pairs"]) and st["mode"] == "selected" and not self.apply_mode():
            return
        self.go(3)

    # ================================================================== listening
    def _ensure_audio(self):
        if self.sink is not None or not self._audio_wanted:
            return
        try:
            from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

            dev = QMediaDevices.defaultAudioOutput()
            if dev.isNull():
                raise RuntimeError("устройство вывода звука не найдено")
            fmt = QAudioFormat()
            fmt.setSampleRate(RATE)
            fmt.setChannelCount(2)
            fmt.setSampleFormat(QAudioFormat.Int16)
            self.sink = QAudioSink(dev, fmt, self)
            self.sink.setBufferSize(RATE * 4 // 12)  # ~80 ms: switching is heard almost at once
            self.device = make_device(self.core, self)
            self.sink.start(self.device)
        except Exception as exc:
            self._audio_wanted = False
            self.status.showMessage(f"Воспроизведение недоступно: {exc}")

    def refresh_listen(self):
        st = self.state
        self.result = C.evaluate(st, self.settings)
        if self._finished() and not C.next_pair(st):
            self.listen_head.setText("Сравнение завершено")
            self.listen_note.setText("Результат — на экране «Итог».")
            self._show_versions([])
            return
        if self._loaded_for != self.session_id:
            self._load_audio()
            return
        self._ensure_audio()
        pair = C.next_pair(st)
        self._pair = pair
        if st["stage"] == "prelim":
            labels = C.active_labels(st)
            self.listen_head.setText(f"Этап 1 · оцените каждую версию ({'/'.join(labels)})")
            self.listen_note.setText("Версий много, поэтому сначала общая оценка: поставьте баллы каждой, затем "
                                     f"{C.FINALISTS} лучших сравнятся попарно.")
        elif pair:
            labels = [pair["a"], pair["b"]]
            rounds = [p for p in st["pairs"] if p["stage"] == "round"]
            if pair["stage"] == "final":
                self.listen_head.setText(f"ФИНАЛ · {pair['a']} против {pair['b']}")
                self.listen_note.setText("ТРЕБУЕТСЯ ФИНАЛЬНОЕ A/B-СРАВНЕНИЕ: лидеры набрали почти одинаково.")
            else:
                done = len([p for p in rounds if p["result"]])
                self.listen_head.setText(f"Сравнение {done + 1} из {len(rounds)} · {pair['a']} против {pair['b']}")
                self.listen_note.setText("Переключайтесь между вариантами в любой момент — позиция сохраняется. "
                                         "Оценки можно менять до конца теста.")
        else:
            self.go(5)
            return
        self._show_versions(labels)

    def _load_audio(self):
        st, sid = self.state, self.session_id
        labels = C.active_labels(st)
        self.listen_head.setText("Загружаем версии…")
        self.listen_note.setText("Файлы читаются в память, чтобы переключение было мгновенным.")
        self._show_versions([])
        if self._busy:
            return
        self._busy = True

        def work():
            return {lab: C.load_playback(C.version_of(st, lab)["path"]) for lab in labels}

        def done(tracks):
            self._busy = False
            if self.session_id != sid:
                return
            self.core.set_tracks(tracks, C.playback_gains_db(st))
            self.core.volume = self.volume.value() / 100.0
            self._loaded_for = sid
            loop = st["marks"].get("loop")
            if loop:
                self.core.set_loop(int(loop[0] * RATE), int(loop[1] * RATE))
            self.loop_button.setChecked(bool(self.core.loop))
            if self.pages.currentIndex() == 3:
                self.refresh_listen()

        def fail(message):
            self._busy = False
            QMessageBox.warning(self, APP_TITLE, "Не удалось загрузить аудио:\n" + message)
            self.go(2)

        run_bg(self, work, on_done=done, on_fail=fail)

    def _clear(self, layout):
        while layout.count():
            w = layout.takeAt(0).widget()
            if w:
                w.deleteLater()

    def _show_versions(self, labels: list[str]):
        self._shown = labels
        self._clear(self.version_row)
        self._clear(self.verdict_row)
        self.version_buttons = {}
        for i, lab in enumerate(labels):
            b = button(f"{lab}", lambda checked=False, x=lab: self.switch_to(x))
            b.setCheckable(True)
            b.setMinimumHeight(54)
            b.setToolTip(f"Клавиша {i + 1}" if i < 9 else "")
            b.setStyleSheet("font-size:20pt; font-weight:600;")
            self.version_buttons[lab] = b
            self.version_row.addWidget(b)
        self.rating_table.setRowCount(len(C.CRITERIA) if labels else 0)
        self.rating_table.setColumnCount(max(1, len(labels)))
        self.rating_table.setHorizontalHeaderLabels(labels or [""])
        self.rating_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        st = self.state
        for c, lab in enumerate(labels):
            card = st["ratings"].get(lab, {})
            for r, (key, _name) in enumerate(C.CRITERIA):
                sp = QSpinBox()
                sp.setRange(0, 10)
                sp.setSpecialValueText("—")
                sp.setValue(int(card.get(key, 0)))
                sp.valueChanged.connect(lambda v, x=lab, k=key: self.rate(x, k, v))
                self.rating_table.setCellWidget(r, c, sp)
        if not labels:
            return
        self.rating_table.setVerticalHeaderLabels([name for _, name in C.CRITERIA])
        if st["stage"] == "prelim":
            self.verdict_row.addWidget(button("Завершить этап 1 и выбрать лучших  →", self.finish_prelim, "Primary"))
        elif self._pair:
            a, b = self._pair["a"], self._pair["b"]
            self.verdict_row.addWidget(button(f"Лучше {a}", lambda: self.verdict(a), "Primary"))
            self.verdict_row.addWidget(button("Равны", lambda: self.verdict("draw")))
            self.verdict_row.addWidget(button(f"Лучше {b}", lambda: self.verdict(b), "Primary"))
        self.verdict_row.addStretch()
        if self.core.active not in labels:
            self.core.switch(labels[0])
        self._mark_active()

    def _mark_active(self):
        for lab, b in self.version_buttons.items():
            b.setChecked(lab == self.core.active)
        if self.state and self.core.active in self.state["labels"]:
            self.wave.peaks = C.version_of(self.state, self.core.active)["waveform"]
        self.wave.update()

    def switch_to(self, lab: str):
        self.core.switch(lab)
        self._mark_active()

    def _switch_index(self, n: int):
        if self.pages.currentIndex() == 3 and n < len(self._shown):
            self.switch_to(self._shown[n])

    def toggle_play(self):
        if self.pages.currentIndex() != 3 or not self.core.tracks:
            return
        if not self.core.playing and self.core.pos >= self.core.length():
            self.core.seek(0)
        self.core.playing = not self.core.playing

    def _seek_seconds(self, seconds: float):
        self.core.seek(int(max(0.0, seconds) * RATE))

    def _seek_fraction(self, f: float):
        self.core.seek(int(f * self.core.length()))

    def mark_chorus(self):
        self.state["marks"]["chorus_s"] = round(self.core.pos / RATE, 2)
        self.save()
        self.status.showMessage(f"Припев отмечен на {_mmss(self.state['marks']['chorus_s'])}.")

    def jump_chorus(self):
        t = self.state["marks"].get("chorus_s")
        if t is None:
            self.status.showMessage("Сначала дойдите до припева и нажмите «Отметить припев здесь».")
        else:
            self._seek_seconds(t)

    def jump_ending(self):
        self._seek_seconds(max(0.0, self.core.length() / RATE - 30.0))

    def loop_start(self):
        self._loop_start = self.core.pos
        self.status.showMessage(f"Начало петли: {_mmss(self.core.pos / RATE)}. Теперь отметьте конец.")

    def loop_end(self):
        if self._loop_start is None or self.core.pos - self._loop_start < RATE // 2:
            self.status.showMessage("Сначала отметьте начало петли, затем конец (не короче полсекунды).")
            return
        start, end = self._loop_start, self.core.pos
        self.core.set_loop(start, end)
        self.core.seek(start)
        self.state["marks"]["loop"] = [round(start / RATE, 2), round(end / RATE, 2)]
        self.loop_button.setChecked(True)
        self.save()

    def loop_toggle(self):
        loop = self.state["marks"].get("loop") if self.state else None
        if self.loop_button.isChecked() and loop:
            self.core.set_loop(int(loop[0] * RATE), int(loop[1] * RATE))
        else:
            self.core.set_loop(None, None)
            if self.loop_button.isChecked():
                self.loop_button.setChecked(False)
                self.status.showMessage("Петля не задана: отметьте начало и конец.")

    def _tick(self):
        if self.pages.currentIndex() != 3 or not self.core.tracks:
            return
        n = max(1, self.core.length())
        self.wave.pos = min(1.0, self.core.pos / n)
        t = self.state["marks"].get("chorus_s") if self.state else None
        self.wave.chorus = None if t is None else min(1.0, t * RATE / n)
        self.wave.loop = (self.core.loop[0] / n, min(1.0, self.core.loop[1] / n)) if self.core.loop else None
        self.wave.update()
        self.time_label.setText(_mmss(self.core.pos / RATE))
        self.play_button.setText("Пауза" if self.core.playing else "▶  Слушать")

    def rate(self, lab: str, key: str, value: int):
        C.set_rating(self.state, lab, key, value or None)
        self.save()

    def verdict(self, result: str):
        if not self._pair:
            return
        C.record_pair(self.state, self._pair["id"], result)
        self.save()  # evaluate() may add the final pair here
        if C.next_pair(self.state):
            self.refresh_listen()
        else:
            self.core.playing = False
            self.go(5)

    def finish_prelim(self):
        try:
            finalists = C.advance_tournament(self.state, self.settings)
        except C.VersusError as exc:
            QMessageBox.information(self, APP_TITLE, str(exc))
            return
        self.save()
        self.status.showMessage("В финальный круг прошли: " + ", ".join(finalists))
        self.refresh_listen()

    # ================================================================== table / result
    def refresh_table(self):
        st = self.state
        res = self.result = C.evaluate(st, self.settings)
        reveal = self._finished()
        rows = res["standings"]
        self.standings_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            name = C.version_of(st, row["label"])["name"] if reveal else "скрыто до конца теста"
            vals = [row["place"], row["label"], name, row["wins"], row["draws"], row["losses"],
                    "—" if row["rating"] is None else row["rating"], row["penalty"], row["score"]]
            for c, v in enumerate(vals):
                self.standings_table.setItem(r, c, _item(v))
        self.pairs_table.setRowCount(len(st["pairs"]))
        for r, p in enumerate(st["pairs"]):
            verdict = "ещё не проведено" if p["result"] is None else (
                "равны" if p["result"] == "draw" else f"лучше {p['result']}")
            for c, v in enumerate(["Финал" if p["stage"] == "final" else "Круг", f"{p['a']} против {p['b']}", verdict]):
                self.pairs_table.setItem(r, c, _item(v))

    def refresh_result(self):
        st = self.state
        res = self.result = C.evaluate(st, self.settings)
        done = res["status"] in (C.WINNER, C.APPLIED)

        def ver(lab):
            if not lab:
                return "—"
            v = C.version_of(st, lab)
            return f"ВЕРСИЯ {v['id']}  ({v['name']})"

        self.final_button.setVisible(bool(res["needs_final"] and C.next_pair(st)))
        self.confirm_button.setEnabled(done)
        self.override_combo.clear()
        if done or res["needs_final"]:
            for lab in C.active_labels(st):
                self.override_combo.addItem(ver(lab), lab)
            if res.get("winner"):
                self.override_combo.setCurrentIndex(self.override_combo.findData(res["winner"]))
        if done:
            self.result_head.setText(f"ПОБЕДИТЕЛЬ: ВЕРСИЯ {C.version_of(st, res['winner'])['id']}")
            lines = [f"ПОБЕДИТЕЛЬ: {ver(res['winner'])}", f"ВТОРОЕ МЕСТО: {ver(res.get('second'))}",
                     f"УВЕРЕННОСТЬ: {res['confidence']}%" if res.get("confidence") is not None else "УВЕРЕННОСТЬ: —",
                     "ТЕХНИЧЕСКИЙ ПОБЕДИТЕЛЬ: " + (", ".join(f"ВЕРСИЯ {i}" for i in res["technical_winner"]) or "—"),
                     "", "ПОЧЕМУ ТАК:"] + [f"  • {x}" for x in res.get("explanation", [])]
            lines += ["", "КТО БЫЛ КЕМ:"] + [f"  {lab} = {ver(lab)}" for lab in sorted(st["labels"])]
            if res["status"] == C.APPLIED:
                lines += ["", "РЕЗУЛЬТАТ ПРИМЕНЁН. Отчёты сохранены в папке песни."]
            self.result_text.setPlainText("\n".join(lines))
        elif res["needs_final"]:
            self.result_head.setText("ТРЕБУЕТСЯ ФИНАЛЬНОЕ A/B-СРАВНЕНИЕ")
            self.result_text.setPlainText(
                (res.get("note") or "") + "\n\nДва лидера набрали почти одинаковое количество баллов. Проведите "
                "финальное сравнение или выберите победителя вручную — выбор будет записан в отчёт.")
        else:
            self.result_head.setText("Тест ещё не завершён" if st["labels"] else "Нет допущенных версий")
            self.result_text.setPlainText(res.get("note") or "Вернитесь на экран «Прослушивание» и закончите "
                                          "сравнения. Названия версий откроются после завершения.")

    def do_override(self):
        lab = self.override_combo.currentData()
        if not lab:
            return
        res = C.evaluate(self.state, self.settings)
        auto = res.get("auto_winner") or (res.get("winner") if not self.state.get("override") else None)
        C.set_override(self.state, lab, self.override_reason.text(), auto)
        self.save()
        self.refresh_result()

    # ================================================================== apply
    def pick_album(self):
        d = QFileDialog.getExistingDirectory(self, "Папка альбома", self.album_path.text() or self.root or "")
        if d:
            self.album_path.setText(d)
            self.chk_album.setChecked(True)
            self.refresh_apply()

    def _plan(self) -> list[dict]:
        return C.plan_apply(
            self.state, self.result["winner"], rename=self.chk_rename.isChecked(),
            number=self.spin_number.value() or None, title=self.edit_title.text().strip() or None,
            album_folder=self.album_path.text() if self.chk_album.isChecked() and self.album_path.text() else None,
            defer_losers=self.chk_defer.isChecked())

    def refresh_apply(self):
        if not (self.state and self.result and self.result.get("winner")):
            return
        st = self.state
        win = C.version_of(st, self.result["winner"])
        self.apply_head.setText(f"Победитель: ВЕРСИЯ {win['id']} — {win['name']}")
        if not self.edit_title.text():
            self.edit_title.blockSignals(True)
            self.edit_title.setText(st["group"]["name"])
            self.edit_title.blockSignals(False)
        applied = bool(st.get("applied"))
        self.apply_button.setEnabled(not applied)
        if applied:
            ops = st["applied"]["operations"]
            self.apply_preview.setPlainText("РЕЗУЛЬТАТ УЖЕ ПРИМЕНЁН.\n\n" + ("\n".join(
                f"{o['src']}\n   →  {o['dst']}" for o in ops) or "Файлы оставлены на месте.") + "\n\nОтчёты:\n" +
                "\n".join(st["applied"].get("reports", {}).values()))
            return
        ops = self._plan()
        problems = C.check_apply(st, ops)
        text = ["Будет сделано:"] + ([f"  {Path(o['src']).name}\n     →  {o['dst']}" for o in ops] or
                                     ["  файлы остаются на месте"])
        text += ["", f"Отчёты будут сохранены в папке: {st['group']['folder']}",
                 f"  {C.REPORT_TXT}, {C.REPORT_CSV}, {C.REPORT_JSON}"]
        if problems:
            text += ["", "ПРОБЛЕМЫ (применение невозможно):"] + [f"  {p}" for p in problems]
        self.apply_preview.setPlainText("\n".join(text))
        self.apply_button.setEnabled(not problems)

    def do_apply(self):
        ops = self._plan()
        msg = (f"Переместить или переименовать файлов: {len(ops)}. Продолжить?" if ops else
               "Файлы останутся на месте, будет сохранён только отчёт. Продолжить?")
        if QMessageBox.question(self, "ПРИМЕНИТЬ РЕЗУЛЬТАТ", msg) != QMessageBox.Yes:
            return  # cancelled: nothing was touched
        try:
            C.execute_apply(self.state, ops, self.settings, confirmed=True)
        except C.VersusError as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self._loaded_for = None  # files may have moved: reload before any further listening
        self.save()
        C.write_reports(self.state, self.settings)  # final status included
        self.status.showMessage("Результат применён, отчёты сохранены.")
        self.refresh_apply()

    # ================================================================== history
    def refresh_history(self):
        rows = self.store.history()
        self._history = rows
        self.history_table.setRowCount(len(rows))
        for r, h in enumerate(rows):
            vals = [time.strftime("%d.%m.%Y %H:%M", time.localtime(h["updated_at"])), h["song"],
                    h["status"] + (" (архив)" if h["archived"] else ""), h["winner"] or "",
                    f"{h['confidence']}%" if h["confidence"] is not None else "", h["folder"]]
            for c, v in enumerate(vals):
                self.history_table.setItem(r, c, _item(v))

    def open_history_row(self):
        rows = sorted({i.row() for i in self.history_table.selectedIndexes()})
        if rows:
            self.load_session(self._history[rows[0]]["id"])

    # ================================================================== shutdown
    def closeEvent(self, event):  # noqa: N802
        self.core.playing = False
        self.save()
        for th, _ in self._threads:
            if _thread_running(th):
                th.wait(15000)
        if self.sink is not None:
            self.sink.stop()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("SunoReleaseMaster")
    app.setStyleSheet(STUDIO_STYLE)
    w = VersusWindow(folder=argv[0] if argv else None)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
