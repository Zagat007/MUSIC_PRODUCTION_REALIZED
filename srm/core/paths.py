"""Filesystem safety helpers: hashing, Unicode-safe names, atomic/exclusive writes,
versioned output directories and path containment checks.

Source protection: nothing in this module ever opens a pre-existing file for writing.
``atomic_write_*`` writes to a temporary sibling and renames it into place only when the
target does not exist (unless ``overwrite_generated=True`` is given for files the
application itself generated, e.g. reports being refreshed).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from pathlib import Path

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class PathSafetyError(ValueError):
    pass


def sha256_file(path: str | os.PathLike, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:  # read-only
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def safe_name(name: str, max_len: int = 120, fallback: str = "untitled") -> str:
    """Make a filename component safe for Windows while preserving Unicode (e.g. Cyrillic)."""
    name = unicodedata.normalize("NFC", name or "")
    name = _BAD_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if not name:
        name = fallback
    if name.split(".")[0].upper() in _WIN_RESERVED:
        name = "_" + name
    if len(name) > max_len:
        name = name[:max_len].rstrip(". ")
    return name


def ensure_within(path: str | os.PathLike, root: str | os.PathLike) -> Path:
    """Resolve ``path`` and require it to be inside ``root`` (symlinks resolved)."""
    p = Path(path).resolve()
    r = Path(root).resolve()
    try:
        p.relative_to(r)
    except ValueError as exc:
        raise PathSafetyError(f"path is outside the allowed root: {p}") from exc
    return p


def next_version_dir(base: str | os.PathLike, prefix: str = "v") -> Path:
    """Create and return the next unused ``v001``-style directory under ``base`` (exclusive)."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    existing = []
    for child in base.iterdir():
        m = re.fullmatch(rf"{prefix}(\d+)", child.name)
        if m:
            existing.append(int(m.group(1)))
    n = max(existing, default=0) + 1
    while True:
        d = base / f"{prefix}{n:03d}"
        try:
            d.mkdir()
            return d
        except FileExistsError:
            n += 1


def _atomic_write(target: Path, data: bytes, overwrite_generated: bool) -> Path:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite_generated:
        raise FileExistsError(f"refusing to overwrite existing file: {target}")
    fd, tmp = tempfile.mkstemp(prefix=".srm_tmp_", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if overwrite_generated:
            os.replace(tmp, target)
        else:
            # os.link-free exclusive rename: fail if the target appeared meanwhile.
            if target.exists():
                raise FileExistsError(f"refusing to overwrite existing file: {target}")
            os.rename(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def atomic_write_bytes(target: str | os.PathLike, data: bytes, *, overwrite_generated: bool = False) -> Path:
    return _atomic_write(Path(target), data, overwrite_generated)


def atomic_write_text(target: str | os.PathLike, text: str, *, overwrite_generated: bool = False) -> Path:
    return _atomic_write(Path(target), text.encode("utf-8"), overwrite_generated)


def tmp_sibling(target: str | os.PathLike, suffix: str = "") -> Path:
    """Return a unique temp path next to ``target`` (caller renames it into place)."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".srm_tmp_", suffix=suffix, dir=str(target.parent))
    os.close(fd)
    os.unlink(tmp)
    return Path(tmp)


def commit_tmp(tmp: str | os.PathLike, target: str | os.PathLike) -> Path:
    """Move a finished temp file into place; never overwrites an existing target."""
    target = Path(target)
    if target.exists():
        os.unlink(tmp)
        raise FileExistsError(f"refusing to overwrite existing file: {target}")
    os.rename(tmp, target)
    return target


def free_disk_bytes(path: str | os.PathLike) -> int:
    import shutil

    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free
