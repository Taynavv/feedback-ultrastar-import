"""Route-level tests for the backup endpoints (list / restore / delete)."""
import asyncio
import json
import logging
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import routes  # noqa: E402


class FakeApp:
    """Captures registered handlers by path so tests can call them directly."""

    def __init__(self):
        self.handlers = {}

    def _reg(self, path):
        def deco(fn):
            self.handlers[path] = fn
            return fn
        return deco

    def get(self, path):
        return self._reg(path)

    def post(self, path):
        return self._reg(path)

    def websocket(self, path):
        return self._reg(path)


class FakeDB:
    def __init__(self):
        self.puts = []

    def put(self, rel, mtime, size, meta):
        self.puts.append(rel)


def _make_pak(path: Path, title: str, has_vocals: bool = False) -> None:
    arrangements = [{"id": "lead", "name": "Lead", "file": "arrangements/lead.json"}]
    if has_vocals:
        arrangements.append({"id": "vocals", "name": "Vocals",
                             "notation": "notation_vocals.json"})
    manifest = {"title": title, "artist": "Artist", "duration": 10.0,
                "arrangements": arrangements,
                "stems": [{"id": "full", "file": "stems/full.wav"}]}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.yaml", yaml.safe_dump(manifest))
        z.writestr("arrangements/lead.json", json.dumps({"notes": []}))
        z.writestr("stems/full.wav", b"RIFFfake")
        if has_vocals:
            z.writestr("notation_vocals.json",
                       json.dumps({"version": 1, "staves": [], "measures": []}))


@pytest.fixture()
def ctx(tmp_path):
    dlc = tmp_path / "library"
    (dlc / "ultrastar_import").mkdir(parents=True)
    app = FakeApp()
    db = FakeDB()
    routes.setup(app, {
        "get_dlc_dir": lambda: dlc,
        "extract_meta": lambda p: {"title": "x"},
        "meta_db": db,
        "log": logging.getLogger("test"),
        "config_dir": tmp_path / "config",
    })
    return app, db, dlc


def _call(handler, *args):
    return asyncio.run(handler(*args))


BASE = "/api/plugins/ultrastar_import"


def test_backups_list_and_delete(ctx):
    app, db, dlc = ctx
    pak = dlc / "ultrastar_import" / "Song_Artist.feedpak"
    _make_pak(pak, "Song", has_vocals=True)
    bak = dlc / "ultrastar_import" / "Song_Artist.feedpak.pre-merge.bak"
    _make_pak(bak, "Song", has_vocals=False)

    listing = _call(app.handlers[f"{BASE}/backups"])
    assert len(listing["backups"]) == 1
    b = listing["backups"][0]
    assert b["rel"] == "ultrastar_import/Song_Artist.feedpak.pre-merge.bak"
    assert b["display"] == "Artist — Song"
    assert listing["total_size"] == bak.stat().st_size

    res = _call(app.handlers[f"{BASE}/backup_delete"], {"rel": b["rel"]})
    assert res["ok"] and not bak.exists()
    assert _call(app.handlers[f"{BASE}/backups"])["backups"] == []


def test_backup_restore(ctx):
    app, db, dlc = ctx
    pak = dlc / "ultrastar_import" / "Song_Artist.feedpak"
    _make_pak(pak, "Song", has_vocals=True)          # merged state
    bak = dlc / "ultrastar_import" / "Song_Artist.feedpak.pre-merge.bak"
    _make_pak(bak, "Song", has_vocals=False)         # original state
    original = bak.read_bytes()

    res = _call(app.handlers[f"{BASE}/backup_restore"],
                {"rel": "ultrastar_import/Song_Artist.feedpak.pre-merge.bak"})
    assert res["ok"]
    assert pak.read_bytes() == original              # pak reverted
    assert not bak.exists()                          # backup consumed
    assert db.puts == ["ultrastar_import/Song_Artist.feedpak"]  # re-indexed


def test_backup_delete_all(ctx):
    app, db, dlc = ctx
    for i in range(3):
        _make_pak(dlc / "ultrastar_import" / f"S{i}.feedpak.pre-merge.bak", f"S{i}")
    res = _call(app.handlers[f"{BASE}/backup_delete"], {"all": True})
    assert res["ok"] and res["deleted"] == 3
    assert _call(app.handlers[f"{BASE}/backups"])["backups"] == []


def test_backup_path_safety(ctx, tmp_path):
    app, db, dlc = ctx
    # A file outside the library must not be reachable, even with the suffix.
    evil = tmp_path / "evil.feedpak.pre-merge.bak"
    evil.write_bytes(b"x")
    for rel in ("../evil.feedpak.pre-merge.bak",
                "..\\evil.feedpak.pre-merge.bak",
                "ultrastar_import/nonexistent.feedpak.pre-merge.bak",
                "ultrastar_import/Song.feedpak"):          # wrong suffix
        res = _call(app.handlers[f"{BASE}/backup_restore"], {"rel": rel})
        assert res.get("error"), rel
        res = _call(app.handlers[f"{BASE}/backup_delete"], {"rel": rel})
        assert res.get("error"), rel
    assert evil.exists()
