"""PySide6 desktop GUI. Thin front-end over srm.service.engine.Engine (same engine as CLI/MCP).

Interface text is Russian (with English technical terms where standard).
"""

from __future__ import annotations

import json
import shutil
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from srm import APP_NAME, __version__

DARK = """
QWidget{background:#1b1d21;color:#d8dde3;font-family:'Segoe UI';font-size:10pt}
QTabBar::tab{background:#24272c;padding:6px 14px;border:1px solid #30343a}
QTabBar::tab:selected{background:#2f3439;color:#fff}
QPushButton{background:#2d3238;border:1px solid #3d434a;padding:5px 10px;border-radius:3px}
QPushButton:hover{background:#384048}QPushButton:disabled{color:#666}
QHeaderView::section{background:#262a2f;padding:4px;border:0;border-right:1px solid #333}
QTableWidget,QTreeWidget,QPlainTextEdit,QTextBrowser{background:#15171a;gridline-color:#2a2d31;
 selection-background-color:#2f4f6f}
QGroupBox{border:1px solid #30343a;margin-top:12px;padding-top:6px}QGroupBox::title{left:8px;color:#9aa4ae}
QProgressBar{border:1px solid #333;text-align:center}QProgressBar::chunk{background:#2f6fa8}
"""


# ---------------------------------------------------------------- background worker
class Worker(QObject):
    progress = Signal(dict)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs

    def run(self):
        try:
            res = self.fn(*self.args, **self.kwargs)
            self.finished.emit(res)
        except Exception as exc:  # surfaced to the user, never swallowed
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")


class CallbackDelivery(QObject):
    """Always deliver worker callbacks on the GUI thread, including plain Python closures."""

    def __init__(self, owner, done, fail, progress):
        super().__init__(owner)
        self.done_callback, self.fail_callback, self.progress_callback = done, fail, progress

    @Slot(object)
    def done(self, result):
        if self.done_callback:
            self.done_callback(result)

    @Slot(str)
    def fail(self, message):
        self.fail_callback(message)

    @Slot(dict)
    def progress(self, value):
        if self.progress_callback:
            self.progress_callback(value)


def run_bg(owner, fn, *args, on_done=None, on_fail=None, on_progress=None, **kwargs):
    th = QThread(owner)
    w = Worker(fn, *args, **kwargs)
    w.moveToThread(th)
    th.started.connect(w.run)
    delivery = CallbackDelivery(owner, on_done, on_fail or (lambda m: QMessageBox.critical(owner, APP_NAME, m)), on_progress)
    w.finished.connect(delivery.done, Qt.QueuedConnection)
    w.failed.connect(delivery.fail, Qt.QueuedConnection)
    w.progress.connect(delivery.progress, Qt.QueuedConnection)
    w.finished.connect(th.quit)
    w.failed.connect(th.quit)
    th.finished.connect(th.deleteLater)
    th.finished.connect(delivery.deleteLater)
    owner._threads = [(t, wk) for t, wk in owner._threads if _thread_running(t)]  # forget finished workers
    owner._threads.append((th, w))
    th.start()
    return w


def _thread_running(th) -> bool:
    try:
        return th.isRunning()
    except RuntimeError:  # the Qt object was already deleted after the thread finished
        return False


# ---------------------------------------------------------------- waveform widget
class Waveform(QWidget):
    seek = Signal(float)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(110)
        self.peaks: list[float] = []
        self.markers: list[dict] = []
        self.duration = 1.0
        self.pos_s = 0.0
        self.seg = None  # (start, dur) of audition clip

    def set_data(self, peaks, markers, duration):
        self.peaks, self.markers, self.duration = peaks or [], markers or [], max(duration or 1.0, 1e-6)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#191e1f"))
        w, h = self.width(), self.height()
        if self.seg:
            x0 = self.seg[0] / self.duration * w
            p.fillRect(int(x0), 0, int(self.seg[1] / self.duration * w), h, QColor(60, 90, 120, 70))
        if self.peaks:
            p.setPen(QPen(QColor("#b8cbb7"), 1))
            n = len(self.peaks)
            for i, v in enumerate(self.peaks):
                x = int(i * w / n)
                y = int((1 - min(1.0, v)) * h / 2)
                p.drawLine(x, y, x, h - y)
        for m in self.markers:
            sev = m.get("severity")
            p.setPen(QPen(QColor("#ff6b6b" if sev in ("SEVERE", "MODERATE") else "#ffcc66"), 1))
            x = int(m["start_s"] / self.duration * w)
            p.drawLine(x, 0, x, h)
        p.setPen(QPen(QColor("#ffffff"), 1))
        x = int(self.pos_s / self.duration * w)
        p.drawLine(x, 0, x, h)

    def mousePressEvent(self, ev):
        self.seek.emit(ev.position().x() / max(1, self.width()) * self.duration)


# ---------------------------------------------------------------- A-B player
class ABPlayer(QGroupBox):
    """Plays loudness-matched preview clips; switching keeps the playhead (true A-B)."""

    def __init__(self, waveform: Waveform):
        super().__init__("Прослушивание A-B (выравнивание по громкости)")
        self.wave = waveform
        self.clips: dict[str, str] = {}
        self.seg_start = 0.0
        self.available = True
        lay = QHBoxLayout(self)
        self.combo = QComboBox()
        self.play_btn = QPushButton("▶ / ❚❚")
        self.ab_btn = QPushButton("A ⇄ B")
        self.info = QLabel("нет клипов: создайте превью")
        lay.addWidget(QLabel("Клип:"))
        lay.addWidget(self.combo, 2)
        lay.addWidget(self.play_btn)
        lay.addWidget(self.ab_btn)
        lay.addWidget(self.info, 3)
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

            self.player = QMediaPlayer()
            self.out = QAudioOutput()
            self.player.setAudioOutput(self.out)
            self.player.positionChanged.connect(self._pos)
        except Exception as exc:  # multimedia backend missing
            self.player = None
            self.available = False
            self.info.setText(f"Воспроизведение недоступно: {exc}")
        self.play_btn.clicked.connect(self.toggle)
        self.ab_btn.clicked.connect(self.swap)
        self.combo.currentIndexChanged.connect(lambda _: self._load(keep_pos=True))
        self.wave.seek.connect(self.seek_abs)

    def set_clips(self, preview: dict):
        self.clips = {k: v["path"] for k, v in preview["clips"].items()}
        self.seg_start = preview["start_s"]
        self.wave.seg = (preview["start_s"], preview["duration_s"])
        self.wave.update()
        self.combo.blockSignals(True)
        self.combo.clear()
        for k in preview["clips"]:
            name = ('Оригинал' if k == 'source_decoded' else 'MP3-превью' if 'mp3' in k else 'Обработка')
            self.combo.addItem(name, k)
        self.combo.blockSignals(False)
        self.info.setText("Громкость выровнена для честного сравнения")
        self._load(keep_pos=False)

    def _load(self, keep_pos: bool):
        if not self.player or self.combo.count() == 0:
            return
        k = self.combo.currentData()
        if k not in self.clips:
            return
        pos = self.player.position() if keep_pos else 0
        playing = self.player.playbackState() == self.player.PlaybackState.PlayingState
        self.player.setSource(QUrl.fromLocalFile(self.clips[k]))
        self.player.setPosition(pos)
        if playing:
            self.player.play()

    def toggle(self):
        if not self.player:
            return
        if self.player.playbackState() == self.player.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def swap(self):
        if self.combo.count() > 1:
            self.combo.setCurrentIndex((self.combo.currentIndex() + 1) % self.combo.count())

    def seek_abs(self, t: float):
        if self.player and self.wave.seg:
            rel = t - self.seg_start
            if 0 <= rel <= self.wave.seg[1]:
                self.player.setPosition(int(rel * 1000))

    def _pos(self, ms):
        self.wave.pos_s = self.seg_start + ms / 1000
        self.wave.update()


# ---------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self, project: str | None = None):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1400, 860)
        self.engine = None
        self._threads: list = []
        self.current_job: int | None = None
        self._job_running = False  # a job runner started by this window is active
        self._closing = False
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._build_menu()
        self._build_library()
        self._build_analysis()
        self._build_repair()
        self._build_mastering()
        self._build_release()
        self._build_settings()
        self.status = self.statusBar()
        self.job_bar = QProgressBar()
        self.job_bar.setMaximumWidth(320)
        self.job_label = QLabel("")
        self.status.addPermanentWidget(self.job_label)
        self.status.addPermanentWidget(self.job_bar)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_job)
        self.timer.start(700)
        if project:
            self.open_project(project)

    # --- menus
    def _build_menu(self):
        m = self.menuBar().addMenu("Проект")
        for text, fn in (("Открыть / создать проект…", self.pick_project), ("Добавить папку музыки и сканировать…",
                                                                           self.pick_library)):
            a = QAction(text, self)
            a.triggered.connect(fn)
            m.addAction(a)

    def pick_project(self):
        d = QFileDialog.getExistingDirectory(self, "Папка проекта")
        if d:
            self.open_project(d)

    def open_project(self, path: str):
        from srm.service.engine import Engine

        self.engine = Engine(path)
        self.status.showMessage(f"Проект: {self.engine.root}")
        self.refresh_library()
        self.refresh_settings()

    def pick_library(self):
        if not self._need_project():
            return
        d = QFileDialog.getExistingDirectory(self, "Папка с музыкой Suno (только чтение)")
        if d:
            self.scan(d)

    def _need_project(self) -> bool:
        if self.engine is None:
            QMessageBox.information(self, APP_NAME, "Сначала откройте или создайте проект.")
            return False
        return True

    def scan(self, folder: str, wait: bool = False):
        rid = self.engine.add_library_root(folder)
        self.status.showMessage("Сканирование… (исходные файлы только читаются)")
        return run_bg(self, self.engine.scan, rid, on_done=lambda s: (self.refresh_library(),
                      self.status.showMessage(f"Найдено аудио: {s['audio_files']}, новых: {s['new']}, "
                                              f"ошибок ffprobe: {s['probe_failed']}, lossy: {s['lossy']}")))

    # --- library tab
    def _build_library(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.btn_scan = QPushButton("Добавить папку и сканировать…")
        self.btn_analyze = QPushButton("Анализировать выбранные")
        self.btn_convert = QPushButton("CONVERT_ONLY → WAV")
        self.btn_pause = QPushButton("Пауза/Стоп")
        self.btn_resume = QPushButton("Продолжить задание")
        for b in (self.btn_scan, self.btn_analyze, self.btn_convert, self.btn_pause, self.btn_resume):
            bar.addWidget(b)
        bar.addStretch()
        lay.addLayout(bar)
        split = QSplitter()
        self.album_tree = QTreeWidget()
        self.album_tree.setHeaderLabels(["Альбом", "Треков"])
        self.album_tree.itemSelectionChanged.connect(self.refresh_tracks)
        self.track_table = QTableWidget(0, 9)
        self.track_table.setHorizontalHeaderLabels(["ID", "#", "Название", "Кодек", "Тип", "Промпт/текст", "Дубликат",
                                                    "Анализ", "Последняя версия"])
        self.track_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.track_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.track_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.track_table.itemSelectionChanged.connect(self.on_track_selected)
        split.addWidget(self.album_tree)
        split.addWidget(self.track_table)
        split.setSizes([280, 1100])
        lay.addWidget(split)
        self.job_fail = QPlainTextEdit()
        self.job_fail.setReadOnly(True)
        self.job_fail.setMaximumHeight(110)
        self.job_fail.setPlaceholderText("Ошибки по трекам появятся здесь (задание продолжает остальные треки).")
        lay.addWidget(self.job_fail)
        self.btn_scan.clicked.connect(self.pick_library)
        self.btn_analyze.clicked.connect(lambda: self.start_job("analyze", ["analyze"], {}))
        self.btn_convert.clicked.connect(lambda: self.start_job("convert", ["analyze", "convert"], {"target_bits": 24}))
        self.btn_pause.clicked.connect(self.cancel_job)
        self.btn_resume.clicked.connect(self.resume_job)
        self.tabs.addTab(w, "Библиотека альбомов")

    def refresh_library(self):
        if not self.engine:
            return
        self.album_tree.clear()
        root = QTreeWidgetItem(["(все треки)", str(len(self.engine.list_tracks()))])
        root.setData(0, Qt.UserRole, None)
        self.album_tree.addTopLevelItem(root)
        for a in self.engine.list_albums():
            it = QTreeWidgetItem([a["name"], str(a["tracks"])])
            it.setData(0, Qt.UserRole, a["id"])
            self.album_tree.addTopLevelItem(it)
        self.album_combo.clear()
        for a in self.engine.list_albums():
            self.album_combo.addItem(a["name"], a["id"])
        self.refresh_tracks()

    def selected_album(self):
        items = self.album_tree.selectedItems()
        return items[0].data(0, Qt.UserRole) if items else None

    def refresh_tracks(self):
        if not self.engine:
            return
        rows = self.engine.list_tracks(self.selected_album())
        self._library_rows = rows
        self.track_table.setRowCount(len(rows))
        for r, t in enumerate(rows):
            lv = t["latest_version"]
            vals = [t["id"], t["track_no"], t["title"], t["codec"] or "?",
                    "LOSSY" if t["is_lossy"] else ("lossless" if t["is_lossy"] == 0 else "?"),
                    f"{t['text_match_status']} ({t['text_match_confidence'] or 0:.2f})", t["dup_group"] or "",
                    "да" if t["has_analysis"] else "-",
                    f"v{lv['version_no']:03d} {lv['mode']} {lv['status']}" if lv else ""]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 4 and v == "LOSSY":
                    it.setForeground(QColor("#ffcc66"))
                self.track_table.setItem(r, c, it)
        self.track_table.resizeColumnsToContents()

    def selected_track_ids(self) -> list[int]:
        rows = sorted({i.row() for i in self.track_table.selectedIndexes()})
        return [int(self.track_table.item(r, 0).text()) for r in rows]

    # --- jobs
    def start_job(self, kind, stages, params):
        if not self._need_project():
            return
        ids = self.selected_track_ids() or [t["id"] for t in self.engine.list_tracks(self.selected_album())]
        if not ids:
            return
        if self._job_running:
            self.status.showMessage("Задание уже выполняется. Дождитесь его завершения или остановите его.")
            return
        jid = self.engine.create_job(kind, ids, stages, params)
        self._run_job(jid)

    def _run_job(self, jid: int, retry_failed: bool = False):
        if self._job_running:  # one job at a time: a second runner would process the same tracks twice
            self.status.showMessage("Задание уже выполняется. Дождитесь его завершения или остановите его.")
            return False
        self._job_running = True
        self.current_job = jid
        self.job_fail.clear()

        def done(st):
            self._job_running = False
            self._show_job(st)
            self.refresh_tracks()

        def fail(message):
            self._job_running = False
            self._poll_job()
            QMessageBox.critical(self, APP_NAME, message)

        run_bg(self, self.engine.run_job, jid, retry_failed=retry_failed, workers=self.workers_spin.value(),
               on_done=done, on_fail=fail)
        return True

    def _running_threads(self) -> list:
        self._threads = [(t, w) for t, w in self._threads if _thread_running(t)]
        return self._threads

    def closeEvent(self, event):
        """Never destroy a running worker thread: that aborts the process and leaves half-finished renders."""
        for th, _ in self._running_threads():
            th.wait(300)  # a worker that has just delivered its result needs a moment to leave its thread
        if not self._running_threads():
            super().closeEvent(event)
            return
        event.ignore()
        if self._closing:
            return
        if QMessageBox.question(self, APP_NAME, "Обработка ещё идёт. Остановить после текущего трека и закрыть "
                                                "программу?") != QMessageBox.Yes:
            return
        self._closing = True
        if self.engine and self.current_job:
            self.engine.cancel_job(self.current_job)
        self.status.showMessage("Завершаем текущую операцию — окно закроется автоматически…")
        self._close_timer = QTimer(self)
        self._close_timer.timeout.connect(self._close_when_idle)
        self._close_timer.start(300)

    def _close_when_idle(self):
        if not self._running_threads():
            self._close_timer.stop()
            self.close()

    def cancel_job(self):
        if self.engine and self.current_job:
            self.engine.cancel_job(self.current_job)
            self.status.showMessage("Отмена запрошена: текущие треки завершаются, задание можно продолжить.")

    def resume_job(self):
        if self.engine and self.current_job:
            self._run_job(self.current_job, retry_failed=True)

    def _poll_job(self):
        if self.engine and self.current_job:
            try:
                self._show_job(self.engine.job_status(self.current_job))
            except Exception:
                pass

    def _show_job(self, st: dict):
        self.job_bar.setMaximum(max(1, st["total_items"]))
        self.job_bar.setValue(st["done"])
        self.job_label.setText(f"Задание {st['job_id']} {st['status']}: {st['done']}/{st['total_items']}, "
                               f"ошибок {st['failed']}")
        if st["failures"]:
            self.job_fail.setPlainText("\n".join(f"трек {f['track_id']} [{f['stage']}]: {f['error']}"
                                                 for f in st["failures"]))

    # --- analysis tab
    def _build_analysis(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.wave = Waveform()
        self.wave.setMaximumHeight(140)
        lay.addWidget(self.wave)
        self.player = ABPlayer(self.wave)
        self.player.setMaximumHeight(70)
        lay.addWidget(self.player)
        row = QHBoxLayout()
        self.btn_preview = QPushButton("Создать превью A-B (исходник / версия / MP3)")
        self.btn_report = QPushButton("Открыть HTML-отчёт")
        self.style_combo = QComboBox()
        from srm.dsp.presets import PRESETS

        self.style_combo.addItem("(авто: эвристика, LOW confidence)", None)
        for k, v in PRESETS.items():
            self.style_combo.addItem(v["label"], k)
        self.btn_style = QPushButton("Задать стиль вручную")
        for b in (self.btn_preview, self.btn_report, QLabel("Стиль:"), self.style_combo, self.btn_style):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        split = QSplitter()
        self.meter_table = QTableWidget(0, 2)
        self.meter_table.setHorizontalHeaderLabels(["Параметр", "Значение"])
        self.meter_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.defect_table = QTableWidget(0, 6)
        self.defect_table.setHorizontalHeaderLabels(["Тип", "Время, с", "Канал", "Severity", "Confidence", "Действие"])
        self.defect_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.defect_table.cellDoubleClicked.connect(lambda r, c: self.player.seek_abs(
            float(self.defect_table.item(r, 1).text().split("-")[0])))
        self.prompt_view = QPlainTextEdit()
        self.prompt_view.setReadOnly(True)
        split.addWidget(self.meter_table)
        split.addWidget(self.defect_table)
        split.addWidget(self.prompt_view)
        split.setSizes([420, 620, 360])
        lay.addWidget(split, 1)
        self.btn_preview.clicked.connect(self.make_preview)
        self.btn_report.clicked.connect(self.open_report)
        self.btn_style.clicked.connect(self.set_style)
        self.tabs.addTab(w, "Анализ трека")

    def on_track_selected(self):
        ids = self.selected_track_ids()
        if len(ids) != 1 or not self.engine:
            return
        tid = ids[0]
        t = self.engine.track(tid)
        a = self.engine.get_analysis(tid)
        txt = [f"Файл: {t['path']}", f"SHA-256: {t['sha256']}", f"Кодек: {t['codec']} ({t['family']}), "
               f"{t['sample_rate']} Hz, {t['channels']} ch, bits={t['bits']}",
               f"Сопоставление текста: {t['text_match_status']} ({t['text_match_confidence']})"]
        if t["is_lossy"]:
            txt.append("⚠ LOSSY источник: WAV будет транскодом, а не восстановлением качества. Не Hi-Res/ADM.")
        if t["text"]:
            p = t["text"]["parsed"]
            txt += ["", f"Текстовый файл: {t['text']['path']} [{t['text']['encoding']}]",
                    f"Стиль (заявлено): {p.get('style_prompt')}", f"Модель: {p.get('model_version')}",
                    f"Язык (оценка): {p.get('language_guess')}", "", "Текст песни:", p.get("lyrics") or "-",
                    "", "(Промпт = заявленное намерение, не доказательство содержимого аудио.)"]
        self.prompt_view.setPlainText("\n".join(txt))
        self.meter_table.setRowCount(0)
        self.defect_table.setRowCount(0)
        if not a:
            self.wave.set_data([], [], t["duration_s"] or 1)
            return
        m = a["measurements"]
        rows = [("Технический статус", a["qc"]["technical_status"]),
                ("Прослушивание", a["qc"]["listening_signoff"]),
                ("Integrated LUFS", m["loudness"]["integrated_lufs"]), ("LRA, LU", m["loudness"]["lra_lu"]),
                ("Max short-term LUFS", m["loudness"]["short_term_max_lufs"]),
                ("True peak, dBTP", m["peaks"]["true_peak_dbtp"]), ("Sample peak, dBFS", m["peaks"]["sample_peak_dbfs"]),
                ("Межсэмпловые перегрузки", m["peaks"]["intersample_over_count"]),
                ("PLR, dB", m["dynamics"]["plr_db"]), ("Crest, dB", m["dynamics"]["crest_factor_db"]),
                ("DC, dBFS", m["dynamics"]["dc_offset_dbfs_max"]), ("Корреляция L/R", m["stereo"].get("correlation")),
                ("Side/Mid, dB", m["stereo"].get("side_to_mid_db")),
                ("Потеря в моно, LU", m["stereo"].get("mono_downmix_loudness_loss_lu")),
                ("Эфф. полоса, Гц", m["spectral"]["effective_bandwidth_hz"]),
                ("Темп (оценка)", f"{m['tempo'].get('bpm')} ({m['tempo'].get('confidence')}); альтернативы: "
                 + ", ".join(str(c['bpm']) for c in m['tempo'].get('candidates', [])[1:4])),
                ("Стиль (гипотеза)", ", ".join(f"{r['style']} {r['score']}" for r in m["style"]["ranked"][:3])),
                ("Промпт vs аудио", m["style"]["prompt_vs_audio"])]
        rows += [(f"Полоса {k}", v) for k, v in m["spectral"]["band_energy_db_rel"].items()]
        self.meter_table.setRowCount(len(rows))
        for i, (k, v) in enumerate(rows):
            self.meter_table.setItem(i, 0, QTableWidgetItem(k))
            self.meter_table.setItem(i, 1, QTableWidgetItem(str(v)))
        cands = m["defects"]["candidates"]
        listen = m["defects"]["listening_required"]
        self.defect_table.setRowCount(len(cands) + len(listen))
        for i, c in enumerate(cands):
            for j, v in enumerate([c["type"], f"{c['start_s']}-{c['end_s']}", c["channel"], c["severity"],
                                   c["confidence"], c["recommended_action"]]):
                self.defect_table.setItem(i, j, QTableWidgetItem(str(v)))
        for i, f in enumerate(listen, start=len(cands)):
            self.defect_table.setItem(i, 0, QTableWidgetItem(f["type"]))
            self.defect_table.setItem(i, 3, QTableWidgetItem("UNCERTAIN"))
            self.defect_table.setItem(i, 5, QTableWidgetItem(f["status"] + ": " + f["description"]))
        tl = m.get("timelines", {})
        self.wave.set_data(tl.get("waveform", {}).get("peak", []), cands, m["duration_s"])
        self.refresh_versions(tid)

    def make_preview(self):
        ids = self.selected_track_ids()
        if len(ids) != 1:
            return
        tid = ids[0]
        vs = [v for v in self.engine.list_versions(tid) if v["output_path"]]
        vno = vs[-1]["version_no"] if vs else None
        run_bg(self, self.engine.create_preview, tid, vno, duration_s=30.0, on_done=self.player.set_clips)

    def open_report(self):
        ids = self.selected_track_ids()
        if len(ids) == 1:
            d = self.engine._report_dir(self.engine.track(ids[0]))
            htmls = sorted(d.glob("*.html"))
            if htmls:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(htmls[-1])))

    def set_style(self):
        for tid in self.selected_track_ids():
            self.engine.set_style_override(tid, self.style_combo.currentData())
        self.status.showMessage("Стиль задан вручную (ручной выбор всегда важнее эвристики).")

    # --- repair tab
    def _build_repair(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Ремонт применяется только к коротким локальным дефектам. AUTO - только для щелчков "
                             "с HIGH confidence, прошедших проверки, и только после вашего подтверждения."))
        row = QHBoxLayout()
        self.btn_rp = QPushButton("Предложить план ремонта")
        self.btn_rprev = QPushButton("Превью выбранной правки (оригинал/ремонт/разница)")
        self.btn_rapprove = QPushButton("Одобрить выбранные")
        self.btn_rauto = QPushButton("Принять AUTO-набор")
        self.btn_rreject = QPushButton("Отклонить выбранные")
        for b in (self.btn_rp, self.btn_rprev, self.btn_rapprove, self.btn_rauto, self.btn_rreject):
            row.addWidget(b)
        lay.addLayout(row)
        self.repair_table = QTableWidget(0, 8)
        self.repair_table.setHorizontalHeaderLabels(["Edit", "Тип", "Время", "Канал", "Метод", "Проверки", "Решение",
                                                     "Примечание"])
        self.repair_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.repair_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        lay.addWidget(self.repair_table)
        self.not_rep = QPlainTextEdit()
        self.not_rep.setReadOnly(True)
        lay.addWidget(QLabel("Не исправляется автоматически (нужно прослушивание / регенерация / стемы):"))
        lay.addWidget(self.not_rep)
        self.current_plan = None
        self.btn_rp.clicked.connect(self.propose_repairs)
        self.btn_rprev.clicked.connect(self.preview_repair)
        self.btn_rapprove.clicked.connect(lambda: self.decide(approve=True))
        self.btn_rreject.clicked.connect(lambda: self.decide(approve=False))
        self.btn_rauto.clicked.connect(lambda: self.decide(auto=True))
        self.tabs.addTab(w, "Ремонт")

    def propose_repairs(self):
        ids = self.selected_track_ids()
        if len(ids) == 1:
            run_bg(self, self.engine.propose_repairs, ids[0], on_done=lambda p: self.show_plan(ids[0], p))

    def show_plan(self, tid, p):
        self.current_plan = (tid, p["plan_id"])
        eds = p["edits"]
        self.repair_table.setRowCount(len(eds))
        for i, e in enumerate(eds):
            reg = e.get("regression") or {}
            vals = [e["edit_id"], e["type"], f"{e['start_s']}-{e['end_s']}", e["channel"], e["method"],
                    "PASS" if reg.get("passed") else "FAIL", e["decision"], e.get("note") or ""]
            for j, v in enumerate(vals):
                self.repair_table.setItem(i, j, QTableWidgetItem(str(v)))
        self.not_rep.setPlainText("\n".join(f"{n['type']} {n.get('start_s', '')}: {n['status']} - {n.get('reason', '')}"
                                            for n in p["not_repairable"]))

    def _sel_edits(self):
        return [int(self.repair_table.item(r, 0).text()) for r in sorted({i.row() for i in
                                                                          self.repair_table.selectedIndexes()})]

    def preview_repair(self):
        if self.current_plan and self._sel_edits():
            tid, pid = self.current_plan
            run_bg(self, self.engine.preview_repair, tid, pid, self._sel_edits()[0],
                   on_done=lambda info: self.player.set_clips({
                       "clips": {k: {"path": v, "measured_lufs": None, "matching_gain_db": 0.0}
                                 for k, v in info["files"].items()},
                       "start_s": info["clip_start_s"], "duration_s": 3.0, "reference_lufs": "-"}))

    def decide(self, approve=None, auto=False):
        if not self.current_plan:
            return
        tid, pid = self.current_plan
        sel = self._sel_edits()
        try:
            p = self.engine.decide_repairs(tid, pid, approve=sel if approve else [],
                                           reject=sel if approve is False else [], accept_auto=auto)
            self.show_plan(tid, p)
            self.status.showMessage(f"План {pid}: {p['status']}")
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))

    # --- mastering tab
    def _build_mastering(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["SAFE_MASTER", "RESTORE_AND_MASTER"])
        self.policy_combo = QComboBox()
        self.policy_combo.addItems(["preset_window", "preserve", "target"])
        self.target_spin = QDoubleSpinBox()
        self.target_spin.setRange(-30, -5)
        self.target_spin.setValue(-12.0)
        self.ceiling_spin = QDoubleSpinBox()
        self.ceiling_spin.setRange(-6, -0.1)
        self.ceiling_spin.setSingleStep(0.1)
        self.ceiling_spin.setValue(-1.0)
        form.addRow("Режим", self.mode_combo)
        form.addRow("Политика громкости (нет обязательных -14 LUFS)", self.policy_combo)
        form.addRow("Цель LUFS (для 'target')", self.target_spin)
        form.addRow("Потолок true peak, dBTP", self.ceiling_spin)
        lay.addLayout(form)
        row = QHBoxLayout()
        self.btn_propose = QPushButton("Предложить (ничего не применяется)")
        self.btn_apply_chain = QPushButton("Предложить с моей цепочкой (JSON ниже)")
        self.btn_render = QPushButton("Рендер выбранной версии")
        self.btn_approve = QPushButton("Одобрить версию")
        self.btn_signoff = QPushButton("Я прослушал(а) - подпись")
        for b in (self.btn_propose, self.btn_apply_chain, self.btn_render, self.btn_approve, self.btn_signoff):
            row.addWidget(b)
        lay.addLayout(row)
        split = QSplitter()
        self.actions_view = QTextBrowser()
        self.chain_edit = QPlainTextEdit()
        self.chain_edit.setPlaceholderText("Цепочка (JSON). Каждая стадия: enabled=true/false (bypass). Границы "
                                           "параметров проверяются.")
        split.addWidget(self.actions_view)
        split.addWidget(self.chain_edit)
        lay.addWidget(split, 3)
        self.version_table = QTableWidget(0, 5)
        self.version_table.setHorizontalHeaderLabels(["Версия", "Режим", "Статус", "Конфиг", "Файл"])
        self.version_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.version_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        lay.addWidget(self.version_table, 2)
        self.btn_propose.clicked.connect(lambda: self.propose(False))
        self.btn_apply_chain.clicked.connect(lambda: self.propose(True))
        self.btn_render.clicked.connect(self.render_selected)
        self.btn_approve.clicked.connect(lambda: self.approve_selected("APPROVED"))
        self.btn_signoff.clicked.connect(lambda: self.approve_selected("LISTENING_SIGNOFF"))
        self.tabs.addTab(w, "Мастеринг")

    def propose(self, use_chain: bool):
        ids = self.selected_track_ids()
        if not ids:
            return
        overrides = None
        if use_chain:
            try:
                overrides = json.loads(self.chain_edit.toPlainText())
            except json.JSONDecodeError as exc:
                QMessageBox.warning(self, APP_NAME, f"JSON: {exc}")
                return
        out = []
        for tid in ids:
            try:
                p = self.engine.suggest_mastering(
                    tid, mode=self.mode_combo.currentText(), loudness_policy=self.policy_combo.currentText(),
                    target_lufs=self.target_spin.value() if self.policy_combo.currentText() == "target" else None,
                    ceiling_dbtp=self.ceiling_spin.value(), chain_overrides=overrides)
            except Exception as exc:
                QMessageBox.warning(self, APP_NAME, f"Трек {tid}: {exc}")
                continue
            out.append(f"<h3>Трек {tid} → v{p['version_no']:03d} ({p['preset']}: {p['preset_reason']})</h3><ul>")
            for a in p["actions"]:
                out.append(f"<li><b>{a['stage']}</b>: {a['problem']} → {a['remedy']} "
                           f"[{'ВКЛ' if a['enabled'] else 'выкл'}]<br><small>доказательства: {a['evidence']}; риск: "
                           f"{a['risk']}</small></li>")
            out.append("</ul>")
            self.chain_edit.setPlainText(json.dumps(p["chain"], indent=1))
        out.append(f"<p><i>{p['disclaimer'] if out else ''}</i></p>")
        self.actions_view.setHtml("".join(out))
        if len(ids) == 1:
            self.refresh_versions(ids[0])

    def refresh_versions(self, tid):
        vs = self.engine.list_versions(tid)
        self.version_table.setRowCount(len(vs))
        for i, v in enumerate(vs):
            for j, val in enumerate([v["version_no"], v["mode"], v["status"], v["config_hash"], v["output_path"] or ""]):
                self.version_table.setItem(i, j, QTableWidgetItem(str(val)))

    def _sel_version(self):
        ids = self.selected_track_ids()
        rows = sorted({i.row() for i in self.version_table.selectedIndexes()})
        if len(ids) != 1 or not rows:
            QMessageBox.information(self, APP_NAME, "Выберите один трек и версию.")
            return None, None
        return ids[0], int(self.version_table.item(rows[0], 0).text())

    def render_selected(self):
        tid, vno = self._sel_version()
        if tid:
            run_bg(self, self.engine.render_version, tid, vno,
                   on_done=lambda r: (self.refresh_versions(tid), self.status.showMessage(
                       f"Рендер v{vno:03d}: TP {r['output_measurements']['peaks']['true_peak_dbtp']} dBTP, "
                       f"{r['output_measurements']['loudness']['integrated_lufs']} LUFS; проверки: "
                       + ", ".join(f"{k}={v['status']}" for k, v in r['policy_check'].items()))))

    def approve_selected(self, decision):
        tid, vno = self._sel_version()
        if tid:
            try:
                self.engine.approve(tid, vno, decision=decision)
                self.refresh_versions(tid)
            except Exception as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))

    # --- release tab
    def _build_release(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.album_combo = QComboBox()
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(["release_wav", "compat_wav"])
        self.dist_combo = QComboBox()
        from srm.release.profiles import DISTRIBUTOR_RULES

        for k, v in DISTRIBUTOR_RULES.items():
            self.dist_combo.addItem(f"{v['label']} [{v['verification']['status']}]", k)
        self.chk_mp3 = QCheckBox("MP3 320 превью")
        self.chk_mp3.setChecked(True)
        self.chk_flac = QCheckBox("FLAC архив")
        self.chk_draft = QCheckBox("Разрешить DRAFT (неодобренные версии)")
        form.addRow("Альбом", self.album_combo)
        form.addRow("Профиль", self.profile_combo)
        form.addRow("Дистрибьютор (правила)", self.dist_combo)
        form.addRow(self.chk_mp3)
        form.addRow(self.chk_flac)
        form.addRow(self.chk_draft)
        lay.addLayout(form)
        row = QHBoxLayout()
        self.btn_cohesion = QPushButton("Связность альбома")
        self.btn_export = QPushButton("Экспорт… (с подтверждением)")
        self.btn_validate = QPushButton("Проверить последний экспорт")
        self.btn_copy = QPushButton("Скопировать пакет в папку…")
        for b in (self.btn_cohesion, self.btn_export, self.btn_validate, self.btn_copy):
            row.addWidget(b)
        lay.addLayout(row)
        self.release_view = QTextBrowser()
        lay.addWidget(self.release_view)
        self.last_export = None
        self.btn_cohesion.clicked.connect(self.show_cohesion)
        self.btn_export.clicked.connect(self.do_export)
        self.btn_validate.clicked.connect(self.do_validate)
        self.btn_copy.clicked.connect(self.copy_package)
        self.tabs.addTab(w, "Проверка релиза")

    def show_cohesion(self):
        c = self.engine.album_cohesion(self.album_combo.currentData())
        rows = "".join(f"<tr><td>{r.get('track_no')}</td><td>{r.get('title')}</td><td>{r.get('integrated_lufs')}</td>"
                       f"<td>{r.get('rel_loudness_lu')}</td><td>{r.get('rel_brightness_pct')}</td>"
                       f"<td>{r.get('rel_bass_db')}</td><td>{'; '.join(r.get('flags', []))}</td></tr>"
                       for r in c["tracks"])
        self.release_view.setHtml(f"<h3>Связность: медиана {c.get('median_lufs')} LUFS</h3><table border=1>"
                                  "<tr><th>#</th><th>трек</th><th>LUFS</th><th>отн. LU</th><th>яркость %</th>"
                                  f"<th>бас dB</th><th>флаги</th></tr>{rows}</table><p>{c.get('note', '')}</p>")

    def do_export(self):
        album = self.album_combo.currentData()
        if album is None:
            return
        if QMessageBox.question(self, APP_NAME, "Создать пакет релиза? Файлы не публикуются и не отправляются.") \
                != QMessageBox.Yes:
            return
        run_bg(self, self.engine.export_album, album, profile=self.profile_combo.currentText(),
               include_mp3=self.chk_mp3.isChecked(), include_flac=self.chk_flac.isChecked(),
               allow_draft=self.chk_draft.isChecked(), confirm=True, distributor=self.dist_combo.currentData(),
               on_done=self.show_export, on_fail=lambda m: self.release_view.setPlainText("Экспорт не выполнен:\n" + m))

    def show_export(self, r):
        self.last_export = r["path"]
        self.release_view.setHtml(f"<h2>{r['status']}</h2><p>Техника: {r['technical']} · Человеческие этапы: "
                                  f"{r['human_gates']}</p><p>{r['path']}</p><h3>Нерешённое</h3><ul>"
                                  + "".join(f"<li>{u}</li>" for u in r["unresolved"]) + "</ul>")

    def do_validate(self):
        album = self.album_combo.currentData()
        if album is None:
            return
        try:
            v = self.engine.validate_release(album)
        except Exception as exc:
            self.release_view.setPlainText(str(exc))
            return
        lines = [f"{v['overall']} (техника {v['technical']}, люди {v['human_gates']})", v["package"], ""]
        for t in v["tracks"]:
            lines.append(t["file"])
            lines += [f"   {k}: {c['status']}" for k, c in t["checks"].items()]
        self.release_view.setPlainText("\n".join(lines))

    def copy_package(self):
        if not self.last_export:
            return
        d = QFileDialog.getExistingDirectory(self, "Куда скопировать пакет")
        if d:
            dst = Path(d) / (Path(self.last_export).parent.name + "_" + Path(self.last_export).name)
            if dst.exists():
                QMessageBox.warning(self, APP_NAME, f"Уже существует: {dst}")
                return
            shutil.copytree(self.last_export, dst)
            self.status.showMessage(f"Скопировано в {dst}")

    # --- settings tab
    def _build_settings(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 8)
        self.workers_spin.setValue(2)
        form.addRow("Параллельных треков (RAM ~0.5-1 ГБ на трек)", self.workers_spin)
        lay.addLayout(form)
        self.settings_view = QPlainTextEdit()
        self.settings_view.setReadOnly(True)
        lay.addWidget(self.settings_view)
        self.tabs.addTab(w, "Настройки")

    def refresh_settings(self):
        if self.engine:
            self.settings_view.setPlainText(json.dumps(self.engine.health(), ensure_ascii=False, indent=1) +
                                            "\n\nСеть не используется. Аккаунты и оплата не требуются.")


def main(argv: list[str] | None = None) -> int:
    from srm.gui.studio import STUDIO_STYLE, StudioWindow

    argv = sys.argv[1:] if argv is None else argv
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyleSheet(STUDIO_STYLE)
    w = StudioWindow(argv[0] if argv else None)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
