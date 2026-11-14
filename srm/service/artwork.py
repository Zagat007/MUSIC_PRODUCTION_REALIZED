"""Versioned album artwork and descriptions shared by desktop, CLI and MCP."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from srm.core.paths import ensure_within, sha256_file
from srm.ingest.textmatch import read_text
from srm.release.artwork import attach_cover, generate_abstract_cover, normalize_cover


class ArtworkMixin:
    def album_artwork(self, album, *, verify=True) -> dict:
        aid = self.album_id(album)
        row = self.db.one('SELECT * FROM albums WHERE id=?', (aid,))
        meta = json.loads(row['metadata_json'] or '{}')
        description = meta.get('album_description')
        if description is None and row['folder_hint']:
            concept = Path(row['folder_hint']) / 'КОНЦЕПЦИЯ.txt'
            if concept.is_file() and concept.stat().st_size < 100_000:
                description = read_text(concept)[0].strip().lstrip('\ufeff')
        cover = dict(meta['artwork']) if meta.get('artwork') else None
        if cover:
            path = ensure_within(self.root / cover['file'], self.root)
            if verify and (not path.is_file() or sha256_file(path) != cover['sha256']):
                raise ValueError('Файл обложки изменён или отсутствует; выберите обложку заново')
            cover['path'] = str(path)
        return {'album_id': aid, 'description': description or '', 'cover': cover,
                'history': meta.get('artwork_history', [])}

    def set_album_description(self, album, description: str) -> dict:
        if not isinstance(description, str) or len(description) > 10000:
            raise ValueError('Описание должно быть текстом до 10 000 символов')
        self.set_album_metadata(album, {'album_description': description.strip()})
        self.db.audit('album_description', {'album_id': self.album_id(album)})
        return self.album_artwork(album, verify=False)

    def _make_artwork(self, album, source=None) -> dict:
        aid = self.album_id(album)
        info = self.album_artwork(aid, verify=False)
        identity = uuid.uuid4().hex
        directory = self.root / 'artwork' / str(aid) / identity
        directory.mkdir(parents=True)
        out = directory / 'cover.jpg'
        if source:
            normalize_cover(Path(source).resolve(), out)
            provenance = {'method': 'user_import', 'note': 'Square center crop, normalized to RGB JPEG.'}
        else:
            provenance = generate_abstract_cover(info['description'], out, identity)
        record = {'id': identity, 'file': str(out.relative_to(self.root)).replace('\\', '/'),
                  'sha256': sha256_file(out), 'width': 3000, 'height': 3000, 'created_at': time.time(),
                  'description_sha256': hashlib.sha256(info['description'].encode()).hexdigest(),
                  'description': info['description'], **provenance}
        # Re-read metadata after generation; a concurrently edited description must not be overwritten.
        with self.db.transaction():
            row = self.db.one('SELECT metadata_json FROM albums WHERE id=?', (aid,))
            meta = json.loads(row['metadata_json'] or '{}')
            meta.setdefault('artwork_history', []).append(record)
            meta['artwork'] = record
            self.db.execute('UPDATE albums SET metadata_json=? WHERE id=?',
                            (json.dumps(meta, ensure_ascii=False), aid))
        self.db.audit('album_artwork', {'album_id': aid, 'id': identity, 'method': record['method']})
        return self.album_artwork(aid)

    def import_album_cover(self, album, source: str | Path) -> dict:
        result = self._make_artwork(album, source=source)
        result['previews'] = self.refresh_album_preview_artwork(album)
        return result

    def generate_album_cover(self, album) -> dict:
        result = self._make_artwork(album)
        result['previews'] = self.refresh_album_preview_artwork(album)
        return result

    def select_album_cover(self, album, cover_id: str) -> dict:
        aid = self.album_id(album)
        row = self.db.one('SELECT metadata_json FROM albums WHERE id=?', (aid,))
        meta = json.loads(row['metadata_json'] or '{}')
        chosen = next((c for c in meta.get('artwork_history', []) if c['id'] == cover_id), None)
        if chosen is None:
            raise ValueError('Вариант обложки не найден')
        path = ensure_within(self.root / chosen['file'], self.root)
        if not path.is_file() or sha256_file(path) != chosen['sha256']:
            raise ValueError('Файл выбранного варианта изменён или отсутствует')
        meta['artwork'] = chosen
        self.db.execute('UPDATE albums SET metadata_json=? WHERE id=?', (json.dumps(meta, ensure_ascii=False), aid))
        result = self.album_artwork(aid)
        result['previews'] = self.refresh_album_preview_artwork(aid)
        self.db.audit('album_cover_selected', {'album_id': aid, 'id': cover_id})
        return result

    def refresh_album_preview_artwork(self, album) -> dict:
        info = self.album_artwork(album)
        if not info['cover']:
            raise ValueError('У альбома пока нет обложки')
        count, errors = 0, []
        for track in self.list_tracks(album):
            for version in self.list_versions(track['id']):
                if not version['output_path']:
                    continue
                wav = Path(version['output_path'])
                mp3 = wav.with_name(wav.stem + '_codec_check.mp3')
                if mp3.is_file():
                    try:
                        attach_cover(mp3, Path(info['cover']['path']))
                        count += 1
                    except Exception as exc:
                        errors.append({'track_id': track['id'], 'error': str(exc)})
        self.db.audit('preview_artwork', {'album_id': info['album_id'], 'updated': count, 'errors': len(errors)})
        return {'updated': count, 'errors': errors}
