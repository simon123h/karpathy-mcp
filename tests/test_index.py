import os
import sqlite3
import time

import pytest

from server import NotesIndex, default_db_path


@pytest.fixture
def notes(tmp_path):
    d = tmp_path / "notes"
    d.mkdir()
    return d


@pytest.fixture
def make_index(tmp_path, notes):
    def make(**kw):
        return NotesIndex(notes, tmp_path / "cache" / "t.db", **kw)
    return make


def write(path, text, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_pragmas(make_index):
    idx = make_index()
    assert idx.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert idx.db.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_initial_sync_and_search(make_index, notes):
    write(notes / "a.md", "# Alpha\nthe quick brown fox")
    write(notes / "sub" / "b.md", "no heading here")
    idx = make_index()
    assert idx.sync() == {"added": 2, "updated": 0, "removed": 0, "total": 2}
    assert [r[0] for r in idx.search("fox", 10)] == ["a.md"]
    assert dict(idx.list("")) == {"a.md": "Alpha", "sub/b.md": "b"}  # title falls back to stem


def test_non_markdown_and_hidden_dirs_ignored(make_index, notes):
    write(notes / "a.txt", "fox")
    write(notes / ".git" / "x.md", "fox")
    assert make_index().sync()["total"] == 0


def test_diacritics_folded(make_index, notes):
    write(notes / "a.md", "café")
    assert len(make_index().search("cafe", 10)) == 1


def test_title_ranks_above_body(make_index, notes):
    write(notes / "body.md", "# Other\nzebra")
    write(notes / "title.md", "# Zebra\nstuff")
    assert make_index().search("zebra", 10)[0][0] == "title.md"


def test_unchanged_files_not_reread(make_index, notes, monkeypatch):
    write(notes / "a.md", "one", mtime=1000)
    idx = make_index()
    idx.sync()
    reads = []
    orig = type(notes).read_text
    monkeypatch.setattr(type(notes), "read_text",
                        lambda self, *a, **k: reads.append(self) or orig(self, *a, **k))
    assert idx.sync() == {"added": 0, "updated": 0, "removed": 0, "total": 1}
    assert reads == []


def test_modified_and_deleted(make_index, notes):
    write(notes / "a.md", "oldword", mtime=1000)
    write(notes / "b.md", "keep", mtime=1000)
    idx = make_index()
    idx.sync()
    write(notes / "a.md", "newword", mtime=2000)
    (notes / "b.md").unlink()
    assert idx.sync() == {"added": 0, "updated": 1, "removed": 1, "total": 1}
    assert idx.search("oldword", 10) == []
    assert len(idx.search("newword", 10)) == 1
    assert idx.search("keep", 10) == []
    assert idx.db.execute("SELECT count(*) FROM file_meta").fetchone()[0] == 1
    assert idx.db.execute("SELECT count(*) FROM notes_fts").fetchone()[0] == 1


def test_invalid_utf8_replaced(make_index, notes):
    (notes / "a.md").write_bytes(b"# T\nbad \xff\xfe bytes findme")
    idx = make_index()
    idx.sync()
    assert len(idx.search("findme", 10)) == 1


def test_resolve_rejects_traversal(make_index, notes, tmp_path):
    idx = make_index()
    (tmp_path / "secret.md").write_text("x")
    for bad in ["../secret.md", "sub/../../secret.md", str(tmp_path / "secret.md")]:
        with pytest.raises(ValueError):
            idx.resolve(bad)
    assert idx.resolve("a/b.md") == notes.resolve() / "a" / "b.md"


def test_resolve_rejects_symlink_escape(make_index, notes, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.md").write_text("x")
    (notes / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        make_index().resolve("link/s.md")


def test_invalid_fts_query_raises(make_index):
    with pytest.raises(sqlite3.OperationalError):
        make_index().search('"unbalanced', 10)


def test_list_prefix_escapes_wildcards(make_index, notes):
    write(notes / "a_b" / "x.md", "x")
    write(notes / "aXb" / "y.md", "y")
    idx = make_index()
    idx.sync()
    assert [p for p, _ in idx.list("a_b")] == ["a_b/x.md"]


def test_lazy_sync_respects_interval(make_index, notes):
    idx = make_index(sync_interval=3600)
    idx.sync()
    write(notes / "new.md", "latecomer")
    assert idx.search("latecomer", 10) == []  # inside the window
    idx.last_sync -= 3601
    assert len(idx.search("latecomer", 10)) == 1


def test_default_db_path_sanitizes(tmp_path):
    p = default_db_path("my notes/../x", tmp_path)
    assert p.name == "my_notes_.._x.db" and p.parent.name == "mcp-markdown"
    assert default_db_path("///", tmp_path).stem != ""
