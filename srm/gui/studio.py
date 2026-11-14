"""Focused desktop workflow; the original detailed tools remain accessible on demand."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from srm.gui.app import MainWindow, run_bg

STUDIO_STYLE = """
QWidget { background:#121516; color:#e9e7e0; font-family:'Segoe UI'; font-size:10pt; }
QFrame#Sidebar { background:#191d1e; border-right:1px solid #2a3031; }
QLabel { background:transparent; }
QLabel#Brand { font-size:18pt; font-weight:600; letter-spacing:2px; color:#e5c5a5; }
QLabel#Title { font-size:29pt; font-weight:600; color:#f5f2e9; }
QLabel#Kicker { color:#bca58d; font-size:9pt; letter-spacing:2px; }
QLabel#Muted { color:#929d9d; }
QLabel#CardValue { font-size:25pt; font-weight:500; color:#f5f2e9; }
QLabel#SectionTitle { font-size:15pt; font-weight:600; }
QLabel#TrackTitle { font-size:22pt; font-weight:600; }
QLabel#Badge { color:#a8cfb9; background:#20322b; border:1px solid #304b3d; border-radius:12px; padding:5px 12px; }
QFrame#Card { background:#1b2021; border:1px solid #2c3435; border-radius:14px; }
QPushButton { background:#232a2b; border:1px solid #384141; border-radius:8px; padding:10px 16px; font-weight:500; }
QPushButton:hover { background:#303a3a; border-color:#75847f; }
QPushButton:pressed { background:#3b4844; }
QPushButton:disabled { color:#6c7675; background:#1d2324; border-color:#293132; }
QPushButton#Primary { background:#d9b997; color:#1e201e; border-color:#d9b997; font-weight:600; }
QPushButton#Primary:hover { background:#edcfac; }
QPushButton#Primary:disabled { background:#51483f; color:#938573; border-color:#51483f; }
QPushButton#Quiet { background:transparent; border:0; color:#aab3b0; text-align:left; }
QPushButton#Nav { text-align:left; border:0; background:transparent; color:#a1aaa7; padding:14px 17px; }
QPushButton#Nav:checked { background:#2d3733; color:#e3d1ba; border-left:3px solid #d9b997; }
QPushButton#Nav:hover { background:#262e2c; }
QTableWidget,QTreeWidget { background:#191e1f; border:0; gridline-color:#252d2e; selection-background-color:#35433e;
 selection-color:#fff7eb; alternate-background-color:#1c2223; outline:0; }
QTableWidget::item { padding:8px; border-bottom:1px solid #262d2e; }
QTreeWidget::item { padding:10px 7px; border:0; }
QTreeWidget::item:selected { background:#35433e; border-radius:6px; }
QHeaderView::section { background:#1e2526; color:#94a29c; padding:11px 8px; border:0; font-weight:500; }
QScrollBar:vertical { width:8px; background:transparent; margin:0; }
QScrollBar::handle:vertical { background:#44514c; border-radius:4px; min-height:28px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical { background:transparent; }
QSplitter::handle { background:#121516; width:14px; }
QGroupBox { background:#1b2021; border:1px solid #2c3435; border-radius:10px; margin-top:16px; padding:12px; }
QGroupBox::title { subcontrol-origin:margin; left:14px; color:#b8c4bc; }
QComboBox,QSpinBox,QDoubleSpinBox { background:#22292a; border:1px solid #384141; border-radius:6px; padding:7px; }
QPlainTextEdit,QTextBrowser { background:#191e1f; border:1px solid #30393a; border-radius:8px; padding:8px; }
QTabWidget::pane { border:0; }
QTabBar::tab { background:#1b2222; padding:10px 14px; color:#9daaa3; }
QTabBar::tab:selected { background:#35433e; color:#f3e5d5; }
QProgressBar { background:#242c2b; border:0; border-radius:4px; text-align:center; color:#c8d4cc; min-height:7px; }
QProgressBar::chunk { background:#afc1ad; border-radius:4px; }
QStatusBar { background:#161b1c; color:#a3afa8; border-top:1px solid #293132; }
QMenuBar { background:#191d1e; color:#a9b3ae; }
QMenu { background:#202728; border:1px solid #3a4441; }
QToolTip { background:#303a36; color:#f2e9df; border:1px solid #617367; padding:8px; }
"""


def label(text, kind=None):
    w = QLabel(text)
    if kind:
        w.setObjectName(kind)
    w.setWordWrap(True)
    return w


def button(text, callback, kind=None):
    w = QPushButton(text)
    if kind:
        w.setObjectName(kind)
    w.setCursor(Qt.PointingHandCursor)
    w.clicked.connect(callback)
    return w


def card(title, value, detail):
    box = QFrame()
    box.setObjectName('Card')
    lay = QVBoxLayout(box)
    lay.setContentsMargins(22, 18, 22, 18)
    lay.addWidget(label(title, 'Muted'))
    number = label(value, 'CardValue')
    lay.addWidget(number)
    lay.addWidget(label(detail, 'Muted'))
    return box, number


class StudioWindow(MainWindow):
    def __init__(self, project=None):
        super().__init__(None)
        self.setMinimumSize(1120, 740)
        self.resize(1440, 920)
        self.setStyleSheet(STUDIO_STYLE)
        self._preview_identity = None
        self._selected_identity = None
        self._busy = False
        self._album_job_active = False
        self._artwork_album = None
        self._artwork_engine = None
        self._description_dirty = False
        self.description_timer = QTimer(self)
        self.description_timer.setSingleShot(True)
        self.description_timer.timeout.connect(self.save_album_description)
        self._build_studio()
        self.job_bar.hide()
        self.stop_button = button('Остановить', self.cancel_job)
        self.stop_button.hide()
        self.status.addPermanentWidget(self.stop_button)
        if project:
            self.open_project(project)
        self.refresh_overview()

    def _build_studio(self):
        self.tabs.setParent(None)
        shell = QWidget()
        outer = QHBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName('Sidebar')
        sidebar.setFixedWidth(220)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(20, 30, 20, 22)
        side.setSpacing(9)
        side.addWidget(label('S R M', 'Brand'))
        side.addWidget(label('SUNO RELEASE MASTER', 'Muted'))
        side.addSpacing(42)
        side.addWidget(label('ВАША СТУДИЯ', 'Kicker'))
        self.nav_buttons = []
        for i, text in enumerate(('01   Моя музыка', '02   Прослушивание', '03   Релиз')):
            b = button(text, lambda checked=False, n=i: self.go(n), 'Nav')
            b.setCheckable(True)
            self.nav_buttons.append(b)
            side.addWidget(b)
        side.addStretch()
        side.addWidget(button('Все инструменты', lambda: self.go(3), 'Quiet'))
        self.open_button = button('Открыть другой проект', self.pick_project, 'Quiet')
        side.addWidget(self.open_button)
        side.addSpacing(14)
        side.addWidget(label('●  Работает на компьютере', 'Muted'))
        side.addWidget(label('Оригиналы сохранены', 'Muted'))
        outer.addWidget(sidebar)
        main = QVBoxLayout()
        main.setContentsMargins(34, 28, 34, 20)
        main.setSpacing(18)
        head = QHBoxLayout()
        self.project_caption = label('ЛИЧНАЯ АУДИОСТУДИЯ', 'Kicker')
        head.addWidget(self.project_caption, 1)
        badge = label('●  Локально · без подписки', 'Badge')
        badge.setWordWrap(False)
        head.addWidget(badge)
        main.addLayout(head)
        self.page_title = label('', 'Title')
        self.page_subtitle = label('', 'Muted')
        main.addWidget(self.page_title)
        main.addWidget(self.page_subtitle)
        self.pages = QStackedWidget()
        self._music_page()
        self._listen_page()
        self._release_page()
        self.pages.addWidget(self.tabs)
        # Full measurements and manual tools stay available, without duplicating the library screen.
        self.tabs.setTabVisible(0, False)
        self.tabs.setCurrentIndex(1)
        main.addWidget(self.pages, 1)
        outer.addLayout(main, 1)
        self.setCentralWidget(shell)
        self.go(0)

    def _music_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(20)
        album_card = QFrame()
        album_card.setObjectName('Card')
        details = QHBoxLayout(album_card)
        details.setContentsMargins(18, 16, 18, 16)
        details.setSpacing(22)
        self.cover_image = label('Обложка\nальбома', 'Muted')
        self.cover_image.setAlignment(Qt.AlignCenter)
        self.cover_image.setFixedSize(148, 148)
        self.cover_image.setStyleSheet('background:#121718; border-radius:8px; border:1px solid #39433f;')
        art_column = QVBoxLayout()
        art_column.addWidget(self.cover_image)
        self.cover_versions = QComboBox()
        self.cover_versions.setFixedWidth(148)
        self.cover_versions.setToolTip('Вернуться к предыдущей обложке')
        self.cover_versions.activated.connect(lambda _: self.artwork_action('select'))
        art_column.addWidget(self.cover_versions)
        details.addLayout(art_column)
        column = QVBoxLayout()
        column.setSpacing(8)
        self.description_edit = QPlainTextEdit()
        self.description_edit.setPlaceholderText('О чём этот альбом? Опишите настроение, образы и историю — они зададут направление обложки.')
        self.description_edit.setFixedHeight(70)
        self.description_edit.textChanged.connect(self.description_changed)
        column.addWidget(self.description_edit)
        actions = QHBoxLayout()
        self.cover_import_button = button('Загрузить обложку', lambda: self.artwork_action('import'))
        self.cover_generate_button = button('Создать абстрактный вариант', lambda: self.artwork_action('generate'))
        self.cover_open_button = button('Открыть', self.open_cover)
        actions.addWidget(self.cover_import_button)
        actions.addWidget(self.cover_generate_button)
        actions.addWidget(self.cover_open_button)
        actions.addStretch()
        column.addLayout(actions)
        self.cover_status = label('Описание сохраняется автоматически. Своя обложка или бесплатный локальный вариант.', 'Muted')
        column.addWidget(self.cover_status)
        stats = QHBoxLayout()
        self.stats = {}
        for key, title, _detail in [('albums', 'В библиотеке', 'Альбомов'),
                                   ('tracks', 'В альбоме', 'Композиций'),
                                   ('ready', 'Подготовлено', 'В выбранном альбоме')]:
            number = label('—')
            self.stats[key] = number
            stats.addWidget(label(title + ':', 'Muted'))
            stats.addWidget(number)
            stats.addSpacing(12)
        stats.addStretch()
        column.addLayout(stats)
        details.addLayout(column, 1)
        lay.addWidget(album_card)
        row = QHBoxLayout()
        row.addWidget(label('Библиотека', 'SectionTitle'), 1)
        row.addWidget(button('Добавить музыку', self.pick_library, 'Primary'))
        row.addWidget(button('Проверить альбом', self.analyze_selected_album))
        lay.addLayout(row)
        self.library_hint = label('Добавьте папку с музыкой. Альбомы появятся здесь автоматически.', 'Muted')
        lay.addWidget(self.library_hint)
        split = QSplitter()
        self.album_tree.setMinimumWidth(235)
        self.album_tree.setColumnWidth(0, 235)
        self.album_tree.setColumnWidth(1, 55)
        self.album_tree.header().setStretchLastSection(False)
        self.album_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.album_tree.header().setSectionResizeMode(1, QHeaderView.Fixed)
        self.album_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.track_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.track_table.setAlternatingRowColors(True)
        self.track_table.setShowGrid(False)
        self.track_table.verticalHeader().hide()
        self.track_table.verticalHeader().setDefaultSectionSize(50)
        self.track_table.setHorizontalHeaderLabels(['ID', '№', 'Композиция', 'Формат', 'Источник',
                                                   'Текст', 'Дубликат', 'Проверка', 'Версия'])
        for col in (0, 5, 6, 8):
            self.track_table.hideColumn(col)
        split.addWidget(self.album_tree)
        split.addWidget(self.track_table)
        split.setSizes([290, 750])
        lay.addWidget(split, 1)
        foot = QHBoxLayout()
        foot.addWidget(label('Выберите композицию и перейдите к прослушиванию.', 'Muted'), 1)
        foot.addWidget(button('К прослушиванию  →', lambda: self.go(1), 'Primary'))
        lay.addLayout(foot)
        self.job_fail.setMaximumHeight(60)
        self.job_fail.hide()
        lay.addWidget(self.job_fail)
        self.pages.addWidget(page)

    def _listen_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(20)
        self.listen_title = label('Выберите композицию в библиотеке', 'TrackTitle')
        self.listen_note = label('Подготовьте обработку и сравните её с оригиналом на одинаковой громкости.', 'Muted')
        lay.addWidget(self.listen_title)
        lay.addWidget(self.listen_note)
        stats = QHBoxLayout()
        self.listen_stats = []
        for title, detail in [('Громкость', 'Измерение исходного файла'), ('Пиковый уровень', 'Запас до перегрузки'),
                              ('Источник', 'Формат вашей записи')]:
            box, value = card(title, '—', detail)
            self.listen_stats.append(value)
            stats.addWidget(box)
        lay.addLayout(stats)
        self.wave.setMinimumHeight(180)
        self.wave.setMaximumHeight(240)
        lay.addWidget(self.wave)
        self.player.setTitle('Сравните на слух')
        self.player.setMaximumHeight(100)
        self.player.info.setWordWrap(True)
        self.player.info.setText('Сначала подготовьте фрагмент для сравнения.')
        self.player.play_btn.setText('Слушать / пауза')
        self.player.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.player.ab_btn.setText('Сравнить A / B')
        lay.addWidget(self.player)
        self.preview_button = button('Подготовить прослушивание', self.prepare_listening, 'Primary')
        self.accept_button = button('Я прослушал — сохранить выбор', self.accept_listening)
        self.accept_button.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.preview_button)
        row.addWidget(self.accept_button)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(label('Бережная обработка по умолчанию. Ручные параметры доступны в дополнительных инструментах.', 'Muted'))
        lay.addStretch()
        self.pages.addWidget(page)

    def _release_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(22)
        top = QHBoxLayout()
        top.addWidget(label('Выберите альбом', 'SectionTitle'))
        top.addWidget(self.album_combo, 1)
        lay.addLayout(top)
        self.release_summary = label('Добавьте альбом в библиотеку.', 'TrackTitle')
        lay.addWidget(self.release_summary)
        box = QFrame()
        box.setObjectName('Card')
        plan = QVBoxLayout(box)
        plan.setContentsMargins(26, 24, 26, 24)
        plan.setSpacing(18)
        plan.addWidget(label('Всё для следующего шага', 'SectionTitle'))
        for text in ('01   WAV для передачи дистрибьютору', '02   MP3 для быстрого прослушивания',
                     '03   Отчёт проверки и сведения о релизе'):
            plan.addWidget(label(text))
        plan.addWidget(label('Пакет сохраняется на компьютере. Публикация выполняется отдельно.', 'Muted'))
        lay.addWidget(box)
        row = QHBoxLayout()
        self.album_prepare_button = button('Подготовить альбом', self.prepare_album)
        self.draft_button = button('Собрать черновик релиза', self.export_draft, 'Primary')
        row.addWidget(self.album_prepare_button)
        row.addWidget(self.draft_button)
        row.addStretch()
        lay.addLayout(row)
        self.release_message = label('После проверки звука останется заполнить данные об авторстве и правах.', 'Muted')
        lay.addWidget(self.release_message)
        self.folder_button = button('Открыть папку релиза', self.open_release_folder)
        self.folder_button.hide()
        lay.addWidget(self.folder_button, alignment=Qt.AlignLeft)
        lay.addStretch()
        self.album_combo.currentIndexChanged.connect(self.refresh_overview)
        self.album_combo.activated.connect(self.select_release_album)
        self.pages.addWidget(page)

    def select_release_album(self, _index):
        aid = self.album_combo.currentData()
        for item in self.album_items():
            if item.data(0, Qt.UserRole) == aid:
                self.album_tree.setCurrentItem(item)
                break

    def album_items(self):
        it = QTreeWidgetItemIterator(self.album_tree)
        while it.value():
            item = it.value()
            if item.data(0, Qt.UserRole) is not None:
                yield item
            it += 1

    def analyze_selected_album(self):
        if not self.engine or self.selected_album() is None:
            return
        ids = [t['id'] for t in self.engine.list_tracks(self.selected_album())]
        if ids:
            self._run_job(self.engine.create_job('analyze', ids, ['analyze'], {}))

    def go(self, index):
        self.pages.setCurrentIndex(index)
        for i, b in enumerate(self.nav_buttons):
            b.setChecked(i == index)
        titles = [('Ваша музыка. Всё на месте.', 'Соберите альбомы и подготовьте их к следующему шагу.'),
                  ('Сначала — послушать.', 'Оригинал и обработка. Спокойное сравнение без лишних настроек.'),
                  ('Готовим ваш релиз.', 'Один альбом. Понятный пакет файлов. Проверка перед отправкой.'),
                  ('Дополнительные инструменты', 'Измерения, локальный ремонт и ручные настройки обработки.')]
        self.page_title.setText(titles[index][0])
        self.page_subtitle.setText(titles[index][1])
        self.refresh_overview()

    def pick_library(self):
        from PySide6.QtWidgets import QFileDialog

        from srm.core.paths import safe_name

        folder = QFileDialog.getExistingDirectory(self, 'Выберите папку с музыкой')
        if not folder:
            return
        if self.engine is None:
            base = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)) / 'Projects'
            self.open_project(str(base / safe_name(Path(folder).name)))
        self.scan(folder)

    def pick_project(self):
        if self._busy or self._album_job_active:
            self.status.showMessage('Дождитесь завершения обработки перед сменой проекта.')
            return
        super().pick_project()

    def refresh_library(self):
        if not self.engine:
            return
        previous = self.selected_album()
        albums = self.engine.list_albums()
        roots = self.engine.db.query('SELECT path FROM library_roots ORDER BY id')
        self.album_tree.blockSignals(True)
        self.album_combo.blockSignals(True)
        self.album_tree.clear()
        self.album_combo.clear()
        folders = {}

        def folder(parent, name, path):
            node = QTreeWidgetItem([name, ''])
            node.setFlags(node.flags() & ~Qt.ItemIsSelectable)
            node.setToolTip(0, str(path))
            if parent:
                parent.addChild(node)
            else:
                self.album_tree.addTopLevelItem(node)
            node.setExpanded(True)
            return node

        root_nodes = []
        for r in roots:
            path = Path(r['path']).resolve()
            root_nodes.append((path, folder(None, path.name or str(path), path)))
        target = None
        for album in albums:
            hint = Path(album['folder_hint']).resolve() if album['folder_hint'] else None
            parent = None
            library_path = ''
            parts = ()
            if hint:
                for base, node in root_nodes:
                    if hint.is_relative_to(base):
                        parent, library_path, parts = node, str(base), hint.relative_to(base).parts
                        break
            if parent is None:
                if 'virtual' not in folders:
                    folders['virtual'] = folder(None, 'Другие альбомы', '')
                parent = folders['virtual']
            for depth, name in enumerate(parts[:-1]):
                key = (library_path, parts[:depth + 1])
                if key not in folders:
                    folders[key] = folder(parent, name, Path(library_path).joinpath(*parts[:depth + 1]))
                parent = folders[key]
            name = parts[-1] if parts else album['name']
            item = QTreeWidgetItem([name, str(album['tracks'])])
            item.setData(0, Qt.UserRole, album['id'])
            item.setData(0, Qt.UserRole + 1, library_path)
            item.setToolTip(0, str(hint) if hint else name)
            parent.addChild(item)
            parent.setExpanded(True)
            self.album_combo.addItem(name, album['id'])
            if target is None or album['id'] == previous:
                target = item
        if target:
            self.album_tree.setCurrentItem(target)
            self.album_combo.setCurrentIndex(self.album_combo.findData(target.data(0, Qt.UserRole)))
        self.album_tree.blockSignals(False)
        self.album_combo.blockSignals(False)
        self.refresh_tracks()
        self.refresh_overview()

    def open_project(self, path):
        super().open_project(path)
        self.status.showMessage('Проект открыт · все изменения сохраняются отдельными версиями')

    def refresh_tracks(self):
        previous = self.selected_track_ids()
        self.track_table.blockSignals(True)
        if self.selected_album() is None:
            self.track_table.setRowCount(0)
            self._library_rows = []
        else:
            super().refresh_tracks()
        if not hasattr(self, 'stats'):
            return
        for r in range(self.track_table.rowCount()):
            codec = self.track_table.item(r, 3).text()
            self.track_table.item(r, 3).setText(self.format_name(codec, self._library_rows[r]['path']))
            kind = self.track_table.item(r, 4).text()
            self.track_table.item(r, 4).setText({'LOSSY': 'Сжатый', 'lossless': 'Без потерь'}.get(kind, 'Не определён'))
            self.track_table.item(r, 7).setText('Проверен' if self.track_table.item(r, 7).text() == 'да' else 'Ожидает')
        self.track_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for col, width in ((1, 40), (3, 115), (4, 100), (7, 100)):
            self.track_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.Fixed)
            self.track_table.setColumnWidth(col, width)
        aid = self.selected_album()
        if aid is not None:
            i = self.album_combo.findData(aid)
            if i >= 0:
                self.album_combo.setCurrentIndex(i)
        if self._library_rows:
            row = next((i for i, track in enumerate(self._library_rows) if track['id'] in previous), 0)
            self.track_table.selectRow(row)
        self.track_table.blockSignals(False)
        self.load_album_artwork()
        self.on_track_selected()
        self.refresh_overview()

    @staticmethod
    def format_name(codec, path=None):
        container = Path(path).suffix.lstrip('.').upper() if path else 'PCM'
        bits = {'pcm_s16le': 16, 'pcm_s24le': 24, 'pcm_f32le': 32}.get(codec)
        return f'{container} · {bits} бит' if bits else str(codec).upper()

    def refresh_overview(self, *_):
        if not hasattr(self, 'release_summary'):
            return
        albums = self.engine.list_albums() if self.engine else []
        aid = self.album_combo.currentData()
        selected = self.engine.list_tracks(aid) if self.engine and aid is not None else []
        ready = sum(bool(t['latest_version'] and t['latest_version']['status'] in ('RENDERED', 'APPROVED')) for t in selected)
        for k, v in [('albums', len(albums)), ('tracks', len(selected)), ('ready', ready)]:
            self.stats[k].setText(str(v))
        self.library_hint.setVisible(not albums)
        current = self.album_tree.currentItem()
        if current and current.data(0, Qt.UserRole) is not None:
            self.project_caption.setText(current.data(0, Qt.UserRole + 1) or 'МОЯ КОЛЛЕКЦИЯ')
            if self.pages.currentIndex() == 0:
                self.page_title.setText(current.text(0))
                self.page_subtitle.setText('Выбранный альбом · проверка, прослушивание и подготовка релиза')
        self.release_summary.setText(f'{len(selected)} композиций · {ready} подготовлено' if selected else 'Добавьте альбом в библиотеку.')
        available = not self._busy and not self._album_job_active
        self.cover_import_button.setEnabled(available and aid is not None)
        self.cover_generate_button.setEnabled(available and aid is not None)
        self.preview_button.setEnabled(available and len(self.selected_track_ids()) == 1)
        self.album_prepare_button.setEnabled(bool(selected) and available)
        self.draft_button.setEnabled(bool(selected) and ready == len(selected) and available)

    def on_track_selected(self):
        super().on_track_selected()
        if not hasattr(self, 'listen_title'):
            return
        ids = self.selected_track_ids()
        tid = ids[0] if len(ids) == 1 else None
        if tid != self._selected_identity:
            self._preview_identity = None
            self.accept_button.setEnabled(False)
            if self.player.player:
                self.player.player.stop()
            self.player.combo.clear()
            self.player.clips = {}
            self.wave.seg = None
            self.wave.pos_s = 0
            self.wave.update()
        self._selected_identity = tid
        self.preview_button.setEnabled(tid is not None and not self._busy and not self._album_job_active)
        if tid is None:
            self.listen_title.setText('Выберите одну композицию в библиотеке')
            for v in self.listen_stats:
                v.setText('—')
            return
        track = self.engine.track(tid)
        self.listen_title.setText(track['title'])
        a = self.engine.get_analysis(tid)
        m = a['measurements'] if a else None
        self.listen_stats[0].setText(f"{m['loudness']['integrated_lufs']} LUFS" if m else 'Не измерено')
        self.listen_stats[1].setText(f"{m['peaks']['true_peak_dbtp']:.1f} dBTP" if m else 'Не измерено')
        self.listen_stats[2].setText(self.format_name(track['codec'], track['path']))
        self.listen_note.setText('Источник сжат: обработка не вернёт потерянные детали.' if track['is_lossy'] else
                                 'Сравнивайте на одинаковой громкости. Решение о звучании — за вами.')

    def _busy_state(self, busy):
        self._busy = busy
        self.preview_button.setEnabled(not busy and len(self.selected_track_ids()) == 1)
        self.open_button.setEnabled(not busy)
        self.album_combo.setEnabled(not busy)
        self.description_edit.setEnabled(not busy)
        self.cover_versions.setEnabled(not busy)
        self.cover_import_button.setEnabled(not busy and self.selected_album() is not None)
        self.cover_generate_button.setEnabled(not busy and self.selected_album() is not None)
        self.preview_button.setText('Готовим фрагмент…' if busy else 'Подготовить прослушивание')
        self.refresh_overview()

    def prepare_listening(self):
        ids = self.selected_track_ids()
        if len(ids) != 1 or self._busy or self._album_job_active:
            return
        tid, engine = ids[0], self.engine
        self._busy_state(True)

        def prepare():
            versions = [v for v in engine.list_versions(tid) if v['output_path'] and v['status'] != 'REJECTED']
            if versions:
                vn = versions[-1]['version_no']
            else:
                vn = engine.suggest_mastering(tid, mode='SAFE_MASTER')['version_no']
                engine.render_version(tid, vn)
            return vn, engine.create_preview(tid, vn, duration_s=30)

        def done(result):
            self._busy_state(False)
            if self.selected_track_ids() == [tid] and self.engine is engine:
                self.on_track_selected()
                self.player.set_clips(result[1])
                self._preview_identity = (tid, result[0])
                self.accept_button.setEnabled(True)
                self.refresh_versions(tid)
                self.go(1)
            self.refresh_overview()

        def fail(message):
            self._busy_state(False)
            QMessageBox.warning(self, 'Не удалось подготовить звук', message)

        run_bg(self, prepare, on_done=done, on_fail=fail)

    def accept_listening(self):
        if not self._preview_identity or self.selected_track_ids() != [self._preview_identity[0]]:
            return
        tid, vn = self._preview_identity
        try:
            self.engine.listening_signoff(tid, vn, note='Подтверждено пользователем на экране прослушивания')
            self.engine.approve(tid, vn, decision='APPROVED')
            self.listen_note.setText('Ваш выбор сохранён. Эта версия отмечена как прослушанная.')
            self.accept_button.setEnabled(False)
        except Exception as exc:
            QMessageBox.warning(self, 'Не удалось сохранить выбор', str(exc))

    def prepare_album(self):
        aid = self.album_combo.currentData()
        if not self.engine or aid is None or self._busy or self._album_job_active:
            return
        ids = [t['id'] for t in self.engine.list_tracks(aid)
               if not t['latest_version'] or t['latest_version']['status'] not in ('RENDERED', 'APPROVED')]
        if not ids:
            self.release_message.setText('Все композиции уже подготовлены. Можно собрать черновик.')
            return
        jid = self.engine.create_job('render', ids, ['render'], {'mode': 'SAFE_MASTER', 'codec_preview': True})
        self._album_job_active = True
        self.refresh_overview()
        self._run_job(jid)

    def _show_job(self, st):
        super()._show_job(st)
        if hasattr(self, 'stats'):
            self._album_job_active = st['status'] in ('RUNNING', 'PENDING')
            self.stop_button.setVisible(self._album_job_active)
            self.job_fail.setVisible(bool(st['failures']))
            self.job_bar.show()
            state = {'COMPLETED': 'Готово', 'CANCELLED': 'Остановлено', 'COMPLETED_WITH_ERRORS': 'Нужна проверка'}.get(st['status'], 'Обработка')
            self.job_label.setText(f"{state}: {st['done']} из {st['total_items']} · ошибок {st['failed']}")
            changed = (st['done'], st['failed'], st['status'])
            if getattr(self, '_summary_job_state', None) != changed:
                self._summary_job_state = changed
                self.refresh_overview()

    def export_draft(self):
        aid = self.album_combo.currentData()
        if not self.engine or aid is None or self._busy or self._album_job_active:
            return
        if QMessageBox.question(self, 'Собрать черновик', 'Сохранить WAV, MP3 и отчёт проверки в папку проекта?') != QMessageBox.Yes:
            return
        self._busy_state(True)
        self.release_message.setText('Собираем файлы и проверяем результат…')

        def done(result):
            self._busy_state(False)
            self.last_export = result['path']
            ok = result['technical'] == 'PASS'
            self.release_message.setText(('Черновик собран. Техническая проверка пройдена.' if ok else
                                          'Пакет собран, но техническая проверка обнаружила ошибки.') +
                                         ' Прослушивание, данные об авторстве и правах проверяются отдельно.')
            self.folder_button.show()

        def fail(message):
            self._busy_state(False)
            self.release_message.setText('Не удалось собрать пакет. Подробности: ' + message)

        run_bg(self, self.engine.export_album, aid, profile='release_wav', include_mp3=True,
               allow_draft=True, confirm=True, on_done=done, on_fail=fail)

    def open_release_folder(self):
        if self.last_export:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_export))

    def description_changed(self):
        if self._artwork_album is not None:
            self._description_dirty = True
            self.cover_status.setText('Сохраняем описание…')
            self.description_timer.start(650)

    def save_album_description(self):
        self.description_timer.stop()
        if not self._description_dirty or self._artwork_engine is None:
            return
        try:
            self._artwork_engine.set_album_description(self._artwork_album, self.description_edit.toPlainText())
            self._description_dirty = False
            self.cover_status.setText('Описание сохранено. Новый вариант обложки будет создан по этому описанию.')
        except Exception as exc:
            self.cover_status.setText('Не удалось сохранить описание: ' + str(exc))

    def load_album_artwork(self, force=False):
        aid = self.selected_album()
        if self.engine is self._artwork_engine and aid == self._artwork_album and not force:
            return
        self.save_album_description()
        self._artwork_album, self._artwork_engine = aid, self.engine
        info = None
        if self.engine and aid is not None:
            try:
                info = self.engine.album_artwork(aid)
            except Exception as exc:
                self.cover_status.setText(str(exc))
        self.description_edit.blockSignals(True)
        self.description_edit.setPlainText(info['description'] if info else '')
        self.description_edit.blockSignals(False)
        self._description_dirty = False
        self.cover_image.clear()
        cover = info['cover'] if info else None
        self.cover_versions.blockSignals(True)
        self.cover_versions.clear()
        for i, variant in enumerate(info['history'] if info else []):
            self.cover_versions.addItem(f'Вариант {i+1}', variant['id'])
        if cover:
            self.cover_versions.setCurrentIndex(self.cover_versions.findData(cover['id']))
        self.cover_versions.blockSignals(False)
        self.cover_versions.setVisible(self.cover_versions.count() > 0)
        if cover:
            pixmap = QPixmap(cover['path'])
            self.cover_image.setPixmap(pixmap.scaled(146, 146, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.cover_status.setText('Обложка альбома · 3000 × 3000 · встраивается в MP3-превью')
        else:
            self.cover_image.setText('Обложка\nальбома')
            if info:
                self.cover_status.setText('Описание сохраняется автоматически. Обложку можно загрузить или создать здесь.')
        self.cover_open_button.setEnabled(bool(cover))
        self.cover_import_button.setEnabled(aid is not None and not self._busy)
        self.cover_generate_button.setEnabled(aid is not None and not self._busy)

    def artwork_action(self, action):
        if self._busy or self._album_job_active or self.selected_album() is None:
            return
        self.save_album_description()
        if self._description_dirty:
            return
        aid, engine = self.selected_album(), self.engine
        args = [aid]
        fn = engine.generate_album_cover
        if action == 'import':
            from PySide6.QtWidgets import QFileDialog

            path, _ = QFileDialog.getOpenFileName(self, 'Выберите обложку альбома', '', 'Изображения (*.jpg *.jpeg *.png *.webp)')
            if not path:
                return
            fn, args = engine.import_album_cover, [aid, path]
        elif action == 'select':
            fn, args = engine.select_album_cover, [aid, self.cover_versions.currentData()]
        self._busy_state(True)
        self.cover_status.setText('Готовим обложку и обновляем MP3-превью…')

        def done(result):
            self._busy_state(False)
            if self.engine is engine and self.selected_album() == aid:
                self.load_album_artwork(force=True)
                update = result.get('previews', {})
                self.cover_status.setText(f"Обложка сохранена · обновлено MP3-превью: {update.get('updated', 0)}" +
                                         (f" · ошибок: {len(update['errors'])}" if update.get('errors') else ''))

        def fail(message):
            self._busy_state(False)
            self.cover_status.setText('Не удалось подготовить обложку: ' + message.splitlines()[0])

        run_bg(self, fn, *args, on_done=done, on_fail=fail)

    def open_cover(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if self.engine and self.selected_album() is not None:
            cover = self.engine.album_artwork(self.selected_album())['cover']
            if cover:
                QDesktopServices.openUrl(QUrl.fromLocalFile(cover['path']))

    def closeEvent(self, event):
        self.save_album_description()
        super().closeEvent(event)
