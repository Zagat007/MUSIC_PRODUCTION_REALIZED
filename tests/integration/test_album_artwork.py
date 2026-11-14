"""Artwork must persist per album and be present in the actual MP3 bytes."""
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from srm.core.paths import sha256_file
from srm.core.tools import ffmpeg_input, get_tools, run
from srm.release.artwork import embedded_cover_hash
from srm.service.engine import Engine


@pytest.fixture
def album(tmp_path):
    lib = tmp_path / 'НА РЕЛИЗ' / '01 — TEST'
    lib.mkdir(parents=True)
    (lib / 'КОНЦЕПЦИЯ.txt').write_text('Ночной город. Тёплый свет отеля и дождь.', 'utf-8')
    t = np.arange(48000 * 4) / 48000
    x = .15 * np.sin(2*np.pi*440*t) * np.minimum(1, t/.1) * np.minimum(1, (4-t)/.1)
    for i in (1, 2):
        sf.write(lib / f'{i:02d} — Track {i}.wav', np.column_stack([x, x]), 48000, subtype='PCM_16')
    e = Engine(tmp_path / 'project')
    e.add_library_root(lib.parent)
    e.scan()
    return e, e.list_albums()[0]['id']


def audio_hash(path):
    p = run([get_tools().ffmpeg, '-nostdin', '-v', 'error', '-i', ffmpeg_input(path),
             '-map', '0:a:0', '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', 'pipe:1'])
    assert p.returncode == 0
    return p.stdout


def test_description_drives_versioned_covers(album, tmp_path):
    e, aid = album
    assert 'Ночной город' in e.album_artwork(aid)['description']
    first = e.generate_album_cover(aid)['cover']
    assert first['method'] == 'local_abstract' and first['palette'] == 'night'
    e.set_album_description(aid, 'Огонь, пламя, fire, пепел')
    second = e.generate_album_cover(aid)['cover']
    assert second['palette'] == 'fire' and first['sha256'] != second['sha256']
    assert Path(first['path']).exists() and len(e.album_artwork(aid)['history']) == 2
    with Image.open(second['path']) as image:
        assert image.size == (3000, 3000) and image.mode == 'RGB'
    own = tmp_path / 'own.png'
    Image.new('RGBA', (200, 100), (20, 140, 70, 180)).save(own)
    before = sha256_file(own)
    imported = e.import_album_cover(aid, own)['cover']
    assert imported['method'] == 'user_import' and sha256_file(own) == before
    # A damaged selected asset is rejected, but can be replaced through a fresh import.
    Path(imported['path']).write_bytes(b'broken')
    with pytest.raises(ValueError):
        e.album_artwork(aid)
    assert e.import_album_cover(aid, own)['cover']['sha256']
    again = Engine(e.root, create=False).album_artwork(aid)
    assert again['description'] == 'Огонь, пламя, fire, пепел' and len(again['history']) == 4


def test_cover_in_all_previews_and_independent_validation(album):
    e, aid = album
    source_hashes = {t['path']: sha256_file(t['path']) for t in e.list_tracks(aid)}
    renders = []
    for track in e.list_tracks(aid):
        proposal = e.suggest_mastering(track['id'], loudness_policy='preserve')
        result = e.render_version(track['id'], proposal['version_no'])
        renders.append(Path(result['codec_preview']['path']))
    audio_before = [audio_hash(p) for p in renders]
    cover = e.generate_album_cover(aid)['cover']
    assert all(embedded_cover_hash(p) == cover['sha256'] for p in renders)
    assert [audio_hash(p) for p in renders] == audio_before  # APIC tagging stream-copies compressed audio
    package = Path(e.export_album(aid, confirm=True, allow_draft=True)['path'])
    mp3s = sorted((package / 'MP3_PREVIEW').glob('*.mp3'))
    assert len(mp3s) == 2 and all(embedded_cover_hash(p) == cover['sha256'] for p in mp3s)
    validation = e.validate_release(aid)
    assert validation['technical'] == 'PASS'
    assert all(t['checks']['embedded_album_cover']['status'] == 'PASS' for t in validation['tracks'])
    exported_cover_hash = sha256_file(package / 'ARTWORK/cover.jpg')
    e.generate_album_cover(aid)
    assert sha256_file(package / 'ARTWORK/cover.jpg') == exported_cover_hash  # finished exports are immutable
    stripped = package / 'no_picture.mp3'
    p = run([get_tools().ffmpeg, '-nostdin', '-v', 'error', '-n', '-i', ffmpeg_input(mp3s[0]), '-map',
             '0:a:0', '-c:a', 'copy', ffmpeg_input(stripped)])
    assert p.returncode == 0
    stripped.replace(mp3s[0])
    invalid = e.validate_release(aid)
    assert invalid['technical'] == 'FAIL'
    assert invalid['tracks'][0]['checks']['embedded_album_cover']['status'] == 'FAIL'
    assert all(sha256_file(path) == sha for path, sha in source_hashes.items())


def test_artwork_gui_description_and_version_selection(album):
    import os
    import time

    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication

    from srm.gui.studio import StudioWindow

    e, aid = album
    app = QApplication.instance() or QApplication([])
    w = StudioWindow(str(e.root))
    w.show()
    app.processEvents()
    assert 'Ночной город' in w.description_edit.toPlainText()
    w.description_edit.setPlainText('Океан, water, дождь и свет')
    w.save_album_description()
    assert e.album_artwork(aid)['description'] == 'Океан, water, дождь и свет'

    def wait():
        deadline = time.monotonic()+60
        while w._busy:
            app.processEvents()
            time.sleep(.02)
            assert time.monotonic() < deadline
        app.processEvents()

    w.artwork_action('generate')
    wait()
    assert w.cover_image.pixmap() and not w.cover_image.pixmap().isNull()
    first = e.album_artwork(aid)['cover']['id']
    w.artwork_action('generate')
    wait()
    assert w.cover_versions.count() == 2
    w.cover_versions.setCurrentIndex(0)
    w.artwork_action('select')
    wait()
    assert e.album_artwork(aid)['cover']['id'] == first
    w.description_edit.setPlainText('Новая история альбома')
    e.create_album('Другой альбом')
    w.refresh_library()
    other = next(item for item in w.album_items() if item.text(0) == 'Другой альбом')
    w.album_tree.setCurrentItem(other)
    app.processEvents()
    assert e.album_artwork(aid)['description'] == 'Новая история альбома'
    deadline = time.monotonic()+5
    import shiboken6
    while any(th.isRunning() for th, _ in w._threads if shiboken6.isValid(th)):
        app.processEvents()
        time.sleep(.02)
        assert time.monotonic() < deadline
    w.close()
