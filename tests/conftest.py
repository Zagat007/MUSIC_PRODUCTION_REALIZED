import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "audio_fixtures"))


@pytest.fixture(scope="session")
def fixture_lib(tmp_path_factory) -> Path:
    from make_fixtures import make_all

    out = tmp_path_factory.mktemp("fx")
    return Path(make_all(out)["library"])


@pytest.fixture(scope="session")
def source_hashes(fixture_lib):
    from srm.core.paths import sha256_file

    return {str(p): sha256_file(p) for p in fixture_lib.rglob("*") if p.is_file()}


@pytest.fixture(scope="session")
def scanned(tmp_path_factory, fixture_lib, source_hashes):
    from srm.service.engine import Engine

    e = Engine(tmp_path_factory.mktemp("proj") / "project")
    e.add_library_root(fixture_lib)
    summary = e.scan()
    return e, summary


def track_by_name(engine, fragment: str) -> dict:
    for t in engine.list_tracks():
        if fragment in t["rel_path"]:
            return t
    raise KeyError(fragment)
