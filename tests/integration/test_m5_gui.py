"""GUI path against fixtures (offscreen Qt). Exercises real widgets + real engine work."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")


def _wait(app, cond, timeout=300):
    t0 = time.time()
    while not cond():
        app.processEvents()
        time.sleep(0.02)
        if time.time() - t0 > timeout:
            raise TimeoutError
    app.processEvents()


def test_gui_end_to_end(tmp_path, fixture_lib):
    from PySide6.QtWidgets import QApplication

    from srm.gui.app import DARK, MainWindow

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(DARK)
    w = MainWindow(str(tmp_path / "gui_proj"))
    w.show()
    lib = fixture_lib / "Альбом Б"
    w.scan(str(lib))
    _wait(app, lambda: w.engine.list_tracks() and w.track_table.rowCount() == 3)
    # Analyse all via the GUI job path (background thread, progress bar)
    w.start_job("analyze", ["analyze"], {})
    _wait(app, lambda: w.engine.job_status(w.current_job)["status"] in ("COMPLETED", "COMPLETED_WITH_ERRORS"))
    _wait(app, lambda: w.job_bar.value() == 3)
    w.refresh_tracks()
    w.track_table.selectRow(0)
    w.on_track_selected()
    assert w.meter_table.rowCount() > 10 and w.wave.peaks
    tid = w.selected_track_ids()[0]
    # Mastering: propose -> render via GUI handlers
    w.propose(False)
    assert "v001" in w.actions_view.toPlainText()
    w.version_table.selectRow(0)
    w.render_selected()
    _wait(app, lambda: w.engine.list_versions(tid)[0]["status"] == "RENDERED")
    # A-B preview through the player widget
    w.make_preview()
    _wait(app, lambda: w.player.combo.count() >= 2)
    w.player.swap()
    assert w.player.combo.currentIndex() == 1
    out = tmp_path / "gui_screens"
    out.mkdir()
    for i in range(w.tabs.count()):
        w.tabs.setCurrentIndex(i)
        app.processEvents()
        w.grab().save(str(out / f"tab_{i}.png"))
    assert len(list(out.glob("*.png"))) == 6
    w.close()
