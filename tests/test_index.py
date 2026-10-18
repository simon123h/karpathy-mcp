import os
import sqlite3
import time

import pytest

from server import NotesIndex, default_db_path, extract_section, parse_note


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


def test_invalid_fts_query_falls_back_to_plain_terms(make_index, notes):
    write(notes / "a.md", "# T\nfoo-bar and baz: qux")
    idx = make_index()
    for q in ['"foo', "foo-bar", "baz: qux", "(foo"]:
        assert [r[0] for r in idx.search(q, 10)] == ["a.md"], q
    assert idx.search("!!!", 10) == []


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


def test_sections_are_separate_hits(make_index, notes):
    write(notes / "a.md", "# Wiki\nintro\n\n## Cooking\npasta recipes\n\n## Travel\nflights")
    idx = make_index()
    idx.sync()
    hits = idx.search("pasta", 10)
    assert [(p, t) for p, t, _ in hits] == [("a.md", "Wiki > Cooking")]
    assert dict(idx.list("")) == {"a.md": "Wiki"}


def test_headings_in_code_fences_and_frontmatter_ignored(make_index, notes):
    write(notes / "a.md", "---\n# yaml comment\ntags: x\n---\n# Real\n```\n# not a heading\n```\nbody")
    title, sections, _ = parse_note((notes / "a.md").read_text(), "a.md")
    assert title == "Real"
    assert [h for lvl, h, _ in sections if lvl] == ["Real"]


def test_extract_section_includes_subsections(notes):
    text = "# T\n## A\na body\n### A1\ndeep\n## B\nb body"
    _, sections, _ = parse_note(text, "t.md")
    out = extract_section(sections, "A")
    assert len(out) == 1 and "deep" in out[0] and "b body" not in out[0]
    assert extract_section(sections, "T > B")[0].endswith("b body")
    assert extract_section(sections, "nope") == []


def test_reindex_replaces_old_sections(make_index, notes):
    write(notes / "a.md", "# T\n## Old\noldword", mtime=1000)
    idx = make_index()
    idx.sync()
    write(notes / "a.md", "# T\n## New\nnewword", mtime=2000)
    idx.sync()
    assert idx.search("oldword", 10) == []
    assert [t for _, t, _ in idx.search("newword", 10)] == ["T > New"]


def test_links_and_backlinks(make_index, notes):
    write(notes / "index.md", "# Index\nsee [[Alpha]] and [b](sub/beta.md) and [[ghost]] and [[alpha|A]]")
    write(notes / "Alpha.md", "# Alpha\n[[sub/Beta#part]]\n```\n[[ignored]]\n```")
    write(notes / "sub" / "beta.md", "# Beta\nback to [i](../index.md)")
    idx = make_index()
    idx.sync()
    out, unresolved, back = idx.links_for("index.md")
    assert out == ["Alpha.md", "sub/beta.md"] and unresolved == ["ghost"]
    assert back == ["sub/beta.md"]
    assert idx.links_for("sub/beta.md")[2] == ["Alpha.md", "index.md"]
    assert idx.links_for("Alpha.md")[1] == []  # [[ignored]] in code fence not a link
    assert idx.links_for("missing.md") is None


def test_backlinks_respect_ambiguous_stems(make_index, notes):
    write(notes / "foo.md", "# Foo")
    write(notes / "a" / "foo.md", "# Foo2")
    write(notes / "x.md", "[[foo]]")
    idx = make_index()
    idx.sync()
    assert idx.links_for("foo.md")[2] == ["x.md"]
    assert idx.links_for("a/foo.md")[2] == []


def test_old_schema_is_rebuilt(tmp_path, notes):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE file_meta (rel_path TEXT PRIMARY KEY, mtime REAL);"
                       "INSERT INTO file_meta VALUES ('a.md', 1000);")
    conn.commit()
    conn.close()
    write(notes / "a.md", "# A\nword", mtime=1000)
    idx = NotesIndex(notes, db)
    assert idx.sync()["added"] == 1  # stale mtime entry was dropped, file reindexed
