"""Local square artwork and lossless tagging of MP3 previews (no inference API)."""
from __future__ import annotations

import hashlib
import json
import random
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from srm.core.tools import ffmpeg_input, get_tools, run


def normalize_cover(source: Path, output: Path) -> None:
    if source.stat().st_size > 30 * 1024 * 1024:
        raise ValueError('Обложка должна быть меньше 30 МБ')
    with Image.open(source) as original:
        if original.width * original.height > 40_000_000:
            raise ValueError('Слишком большое изображение')
        image = ImageOps.exif_transpose(original).convert('RGBA')
        bg = Image.new('RGBA', image.size, '#15191b')
        bg.alpha_composite(image)
        image = ImageOps.fit(bg.convert('RGB'), (3000, 3000), method=Image.Resampling.LANCZOS)
        image.save(output, 'JPEG', quality=94, optimize=True)


def generate_abstract_cover(description: str, output: Path, seed: str) -> dict:
    if not description.strip():
        raise ValueError('Сначала добавьте описание альбома')
    text = description.casefold()
    palettes = [
        ('fire', ('огон', 'плам', 'fire', 'burn', 'пепел'), (18, 13, 23), (225, 108, 51)),
        ('water', ('вод', 'океан', 'дожд', 'water', 'ocean', 'rain', 'liquid'), (8, 23, 37), (96, 183, 178)),
        ('forest', ('лес', 'forest', 'земл', 'earth', 'природ'), (13, 26, 24), (162, 183, 127)),
        ('night', ('ноч', 'night', 'noir', 'темн', 'dark', 'город', 'city'), (13, 17, 28), (194, 145, 182)),
        ('dawn', ('рассвет', 'свет', 'dawn', 'light', 'gold', 'золот'), (27, 25, 31), (220, 185, 128)),
    ]
    scores = [sum(text.count(word) for word in words) for _, words, _, _ in palettes]
    rng = random.Random(int(hashlib.sha256((description + seed).encode()).hexdigest(), 16))
    idx = max(range(len(scores)), key=scores.__getitem__) if max(scores) else rng.randrange(len(palettes))
    mood, _, dark, accent = palettes[idx]
    n = 1000
    yy, xx = np.mgrid[0:n, 0:n] / n
    cx, cy = rng.uniform(.35, .65), rng.uniform(.30, .55)
    glow = np.exp(-((xx-cx)**2 + (yy-cy)**2) / rng.uniform(.045, .12))
    grain = np.random.default_rng(rng.randrange(2**32)).normal(0, 1.8, (n, n, 1))
    pixels = np.clip(np.array(dark) + glow[..., None] * np.array(accent) * .72 + grain, 0, 255).astype('uint8')
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image, 'RGBA')
    if mood == 'water':
        for j in range(65):
            y = 440 + j*8
            points = [(x, y + 24*np.sin(x/180 + j*.23)) for x in range(-10, 1010, 8)]
            draw.line(points, fill=(*accent, 35 + j % 6 * 8), width=2)
    elif mood == 'night' and any(w in text for w in ('город', 'city', 'hotel', 'отель')):
        for j in range(18):
            x = j*65-30
            top = rng.randint(250, 670)
            draw.rectangle((x, top, x+48, 1000), fill=(*dark, 240))
            for y in range(top+14, 960, 24):
                if rng.random() > .45:
                    draw.rectangle((x+12, y, x+16, y+5), fill=(*accent, rng.randint(50, 150)))
    else:
        # Restrained orbital / horizon composition; words control palette and composition family.
        for j in range(48):
            radius = 80 + j*6
            box = (cx*n-radius, cy*n-radius, cx*n+radius, cy*n+radius)
            draw.arc(box, start=185+j*.9, end=355+j*.4, fill=(*accent, 45+j*3), width=2)
        draw.rectangle((0, 710, n, n), fill=(*dark, 190))
        draw.line((110, 710, 890, 710), fill=(*accent, 150), width=2)
    image.resize((3000, 3000), Image.Resampling.LANCZOS).save(output, 'JPEG', quality=94, optimize=True)
    return {'method': 'local_abstract', 'palette': mood, 'seed': seed,
            'note': 'Local procedural artwork guided by description keywords; not a neural illustration.'}


def embedded_cover_hash(mp3: Path) -> str | None:
    tools = get_tools()
    probe = run([tools.ffprobe, '-v', 'error', '-select_streams', 'v', '-show_entries',
                 'stream_disposition=attached_pic', '-of', 'json', ffmpeg_input(mp3)])
    streams = json.loads(probe.stdout).get('streams', []) if probe.returncode == 0 else []
    if not streams or streams[0].get('disposition', {}).get('attached_pic') != 1:
        return None
    data = run([tools.ffmpeg, '-nostdin', '-v', 'error', '-i', ffmpeg_input(mp3), '-map', '0:v:0',
                '-c:v', 'copy', '-frames:v', '1', '-f', 'image2pipe', 'pipe:1'])
    if data.returncode:
        raise RuntimeError('Не удалось прочитать встроенную обложку')
    return hashlib.sha256(data.stdout).hexdigest()


def attach_cover(mp3: Path, cover: Path) -> None:
    """Replace APIC front cover atomically; audio is stream-copied and never re-encoded."""
    tmp = mp3.with_name('.artwork_' + uuid.uuid4().hex + '.mp3')
    try:
        tools = get_tools()
        p = run([tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error', '-n', '-i', ffmpeg_input(mp3),
                 '-i', ffmpeg_input(cover), '-map', '0:a:0', '-map', '1:v:0', '-map_metadata', '0',
                 '-c:a', 'copy', '-c:v', 'copy', '-id3v2_version', '3', '-disposition:v:0', 'attached_pic',
                 '-metadata:s:v', 'title=Album cover', '-metadata:s:v', 'comment=Cover (front)', ffmpeg_input(tmp)])
        if p.returncode:
            raise RuntimeError('Не удалось встроить обложку: ' + p.stderr.decode('utf-8', 'replace')[-300:])
        expected = hashlib.sha256(cover.read_bytes()).hexdigest()
        if embedded_cover_hash(tmp) != expected:
            raise RuntimeError('Проверка встроенной обложки не пройдена')
        tmp.replace(mp3)
    finally:
        tmp.unlink(missing_ok=True)
