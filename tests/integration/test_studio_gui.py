"""Verify the simplified user path with real local processing and no fake readiness."""
import os
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')


def wait_for(app, predicate, timeout=90):
    until = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        time.sleep(.02)
        assert time.monotonic() < until, 'GUI action timed out'
    app.processEvents()


def test_studio_default_is_simple(tmp_path):
    from PySide6.QtWidgets import QApplication

    from srm.gui.studio import StudioWindow

    app = QApplication.instance() or QApplication([])
    w = StudioWindow()
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    assert len(w.nav_buttons) == 3 and w.pages.currentIndex() == 0
    assert not w.chain_edit.isVisible() and not w.target_spin.isVisible()
    assert not w.draft_button.isEnabled()
    assert w.library_hint.isVisible()
    w.go(3)
    w.tabs.setCurrentIndex(3)
    app.processEvents()
    assert w.chain_edit.isVisible() and w.target_spin.isVisible()
    w.close()


def test_worker_closure_returns_to_gui_thread():
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication, QWidget

    from srm.gui.app import run_bg

    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    owner._threads = []
    observed = []
    run_bg(owner, lambda: 42, on_done=lambda value: observed.append((value, QThread.currentThread() == app.thread())))
    wait_for(app, lambda: bool(observed))
    assert observed == [(42, True)]
    wait_for(app, lambda: all(not th.isRunning() for th, _ in owner._threads if __import__('shiboken6').isValid(th)))
    owner.close()


def test_studio_listen_accept_and_export(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox

    from srm.gui.studio import StudioWindow
    from srm.service.engine import Engine

    fs = 48000
    t = np.arange(fs * 4) / fs
    x = .12 * np.sin(2 * np.pi * 440 * t) * np.minimum(1, t / .1) * np.minimum(1, (4 - t) / .1)
    lib = tmp_path / 'music' / '01 — TEST ALBUM'
    lib.mkdir(parents=True)
    for i in (1, 2):
        sf.write(lib / f'{i:02d} — Track {i}.wav', np.column_stack([x, x]), fs, subtype='PCM_16')
    project = tmp_path / 'project'
    e = Engine(project)
    e.add_library_root(lib.parent)
    e.scan()
    app = QApplication.instance() or QApplication([])
    w = StudioWindow(str(project))
    w.show()
    w.track_table.selectRow(0)
    app.processEvents()
    tid = w.selected_track_ids()[0]
    w.go(1)
    assert not w.accept_button.isEnabled()
    w.prepare_listening()
    wait_for(app, lambda: w._preview_identity is not None)
    assert w.wave.peaks and w.player.combo.itemText(0) == 'Оригинал'
    assert w.player.combo.count() >= 2
    vno = w._preview_identity[1]
    assert w.engine.version(tid, vno)['status'] == 'RENDERED'
    w.accept_listening()
    assert w.engine.version(tid, vno)['status'] == 'APPROVED'
    w.track_table.selectRow(1)
    app.processEvents()
    assert w._preview_identity is None and not w.accept_button.isEnabled()
    assert w.player.combo.count() == 0  # cannot approve or play another track's preview
    assert not w.draft_button.isEnabled()  # the other track has no render yet
    w.prepare_album()
    wait_for(app, lambda: not w._album_job_active and w.engine.job_status(w.current_job)['status'] == 'COMPLETED')
    w.refresh_overview()
    assert w.draft_button.isEnabled()
    assert w.engine.version(tid, vno)['status'] == 'APPROVED'
    assert len(w.engine.list_versions(tid)) == 1  # bulk preparation preserves the user's chosen version
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.Yes)
    w.export_draft()
    wait_for(app, lambda: w.last_export is not None)
    assert (Path(w.last_export) / 'WAV_MASTER').is_dir()
    result = w.engine.validate_release(w.album_combo.currentData())
    assert result['technical'] == 'PASS' and result['overall'] == 'DRAFT_NOT_RELEASE_READY'
    wait_for(app, lambda: all(not th.isRunning() for th, _ in w._threads if __import__('shiboken6').isValid(th)))
    w.close()
