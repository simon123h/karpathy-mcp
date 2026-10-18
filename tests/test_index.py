import argparse
import os
import sqlite3
import threading
import time

import pytest

from mcp_markdown.index import NotesIndex
from mcp_markdown.parsing import extract_section, format_outline, parse_extensions, parse_note
from mcp_markdown.server import default_db_path


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


def test_unselected_extensions_and_hidden_dirs_ignored(make_index, notes):
    write(notes / "a.txt", "fox")
    write(notes / ".git" / "x.md", "fox")
    assert make_index(extensions=("md",)).sync()["total"] == 0


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
    monkeypatch.setattr(
        type(notes), "read_text", lambda self, *a, **k: reads.append(self) or orig(self, *a, **k)
    )
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


def symlink(link, target):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")


def test_scan_skips_symlinks_outside_root(make_index, notes, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("# Secret\ntopsecret")
    write(notes / "real.md", "# Real\nvisible")
    symlink(notes / "leak.md", outside)
    symlink(notes / "alias.md", notes / "real.md")  # inside the root: fine
    idx = make_index()
    assert idx.sync()["total"] == 2
    assert [p for p, _ in idx.list("")] == ["alias.md", "real.md"]
    assert idx.search("topsecret", 10) == []


def test_scan_does_not_follow_symlinked_directories(make_index, notes, tmp_path):
    outside = tmp_path / "other"
    write(outside / "x.md", "# X\nelsewhere")
    write(notes / "real" / "y.md", "# Y")
    symlink(notes / "linkdir", outside)
    symlink(notes / "innerlink", notes / "real")
    idx = make_index()
    assert idx.sync()["total"] == 1  # only real/y.md
    assert idx.search("elsewhere", 10) == []


def test_fallback_matches_term_prefixes(make_index, notes):
    write(notes / "a.md", "# T\nmigration plan for UserAuthService")
    idx = make_index()
    for q in ['migra"', "(migra", "migra: plan", "UserAuth-"]:
        assert [r[0] for r in idx.search(q, 10)] == ["a.md"], q
    assert idx.search("migra", 10) == []  # valid syntax is used as written: no implicit prefix


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


def test_stale_index_refreshes_in_background(make_index, notes):
    idx = make_index(sync_interval=3600)
    idx.sync()
    write(notes / "new.md", "latecomer")
    assert idx.search("latecomer", 10) == []  # inside the window
    idx.last_sync -= 3601
    assert (
        idx.search("latecomer", 10) == []
    )  # stale-while-revalidate: this query kicks off the rescan
    assert idx.wait_idle()
    assert len(idx.search("latecomer", 10)) == 1


def test_search_does_not_wait_for_rescan(make_index, notes, monkeypatch):
    write(notes / "a.md", "oldword")
    idx = make_index(sync_interval=0)
    idx.sync()
    gate, started = threading.Event(), threading.Event()
    orig = idx._scan
    monkeypatch.setattr(idx, "_scan", lambda: (started.set(), gate.wait(5), orig())[2])
    t0 = time.monotonic()
    assert len(idx.search("oldword", 10)) == 1
    assert started.wait(2) and time.monotonic() - t0 < 1
    write(notes / "b.md", "newword")
    gate.set()
    assert idx.wait_idle()
    assert len(idx.search("newword", 10)) == 1


def test_background_sync_is_single_flight(make_index, monkeypatch):
    idx = make_index(sync_interval=0)
    gate, calls = threading.Event(), []
    monkeypatch.setattr(idx, "_scan", lambda: (calls.append(1), gate.wait(5), {})[2])
    for _ in range(5):
        idx.maybe_sync()
    gate.set()
    assert idx.wait_idle()
    assert len(calls) == 1


def test_first_search_waits_bounded_for_initial_sync(make_index, notes, monkeypatch):
    write(notes / "a.md", "word")
    idx = make_index(startup_wait=0.05)
    gate = threading.Event()
    orig = idx._scan
    monkeypatch.setattr(idx, "_scan", lambda: (gate.wait(5), orig())[1])
    idx.maybe_sync()
    assert idx.search("word", 10) == [] and idx.initial_sync_pending
    gate.set()
    assert idx.wait_idle() and not idx.initial_sync_pending
    assert len(idx.search("word", 10)) == 1


def test_failed_sync_does_not_retrigger_or_block(make_index, monkeypatch):
    idx = make_index(sync_interval=3600, startup_wait=1)

    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(idx, "_scan", boom)
    assert idx.search("x", 10) == []  # waits for the (failed) first attempt, then returns
    assert idx.wait_idle() and not idx.initial_sync_pending
    assert time.monotonic() - idx.last_sync < 5


def test_default_db_path_sanitizes_and_hashes_the_directory(tmp_path):
    p = default_db_path("my notes/../x", tmp_path)
    assert p.name.startswith("my_notes_.._x_") and p.suffix == ".db"
    assert p.parent.name == "mcp-markdown"
    assert default_db_path("///", tmp_path).name.startswith("notes_")
    assert default_db_path("n", tmp_path) == default_db_path("n", tmp_path)
    assert default_db_path("n", tmp_path / "a") != default_db_path("n", tmp_path / "b")


def test_sections_are_separate_hits(make_index, notes):
    write(notes / "a.md", "# Wiki\nintro\n\n## Cooking\npasta recipes\n\n## Travel\nflights")
    idx = make_index()
    idx.sync()
    hits = idx.search("pasta", 10)
    assert [(p, t) for p, t, _ in hits] == [("a.md", "Wiki > Cooking")]
    assert dict(idx.list("")) == {"a.md": "Wiki"}


def test_headings_in_code_fences_and_frontmatter_ignored(make_index, notes):
    write(
        notes / "a.md", "---\n# yaml comment\ntags: x\n---\n# Real\n```\n# not a heading\n```\nbody"
    )
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
    write(
        notes / "index.md",
        "# Index\nsee [[Alpha]] and [b](sub/beta.md) and [[ghost]] and [[alpha|A]]",
    )
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
    conn.executescript(
        "CREATE TABLE file_meta (rel_path TEXT PRIMARY KEY, mtime REAL);"
        "INSERT INTO file_meta VALUES ('a.md', 1000);"
    )
    conn.commit()
    conn.close()
    write(notes / "a.md", "# A\nword", mtime=1000)
    idx = NotesIndex(notes, db)
    assert idx.sync()["added"] == 1  # stale mtime entry was dropped, file reindexed


def test_get_text_served_from_index(make_index, notes):
    text = "---\ntags: x\n---\n# T\nintro\n\n## A\nbody\n\n### A1\ndeep"
    write(notes / "a.md", text)
    idx = make_index()
    idx.sync()
    (notes / "a.md").unlink()  # prove no disk access is needed
    assert idx.get_text("a.md") == text
    assert idx.get_text("missing.md") is None


def test_get_text_empty_file(make_index, notes):
    write(notes / "e.md", "")
    idx = make_index()
    idx.sync()
    assert idx.get_text("e.md") == ""


def test_format_outline():
    _, sections, _ = parse_note("# T\n## A\n" + "x" * 1500 + "\n### B\nshort", "t.md")
    lines = format_outline(sections).splitlines()
    assert lines[0].startswith("# T (") and lines[1].startswith("## A (1.5k chars)")
    assert lines[2].startswith("### B (") and len(lines) == 3


def test_parse_extensions():
    assert parse_extensions("md, .TXT ,md") == ("md", "txt")
    for bad in ["", "exe", "md,pdf"]:
        with pytest.raises(argparse.ArgumentTypeError):
            parse_extensions(bad)


def test_extensions_select_files(make_index, notes):
    write(notes / "a.md", "mdword")
    write(notes / "b.txt", "txtword")
    write(notes / "c.markdown", "# C\nmdvariant")
    assert make_index().sync()["total"] == 3  # default: every supported extension
    assert make_index(extensions=("md", "markdown")).sync()["total"] == 2
    idx2 = make_index(extensions=("md", "txt"))
    idx2.sync()
    assert [p for p, _ in idx2.list("")] == ["a.md", "b.txt"]
    assert [r[0] for r in idx2.search("txtword", 10)] == ["b.txt"]


def test_plain_text_is_one_section_without_headings():
    title, sections, links = parse_note("# not a heading\n[[x]] body", "notes/My Log.txt")
    assert title == "My Log" and links == set()
    assert sections == [(0, "", "# not a heading\n[[x]] body")]
    assert parse_note("  \n", "e.txt")[1] == []


def test_org_sections_tags_and_blocks():
    text = (
        "#+TITLE: x\n* Top  :work:\nintro\n** Sub\n#+BEGIN_SRC sh\n* not a heading\n#+END_SRC\ntail"
    )
    title, sections, links = parse_note(text, "a.org")
    assert title == "Top" and links == set()
    assert [(lvl, h) for lvl, h, _ in sections] == [(0, ""), (1, "Top"), (2, "Sub")]
    assert "* not a heading" in sections[2][2]


def test_asciidoc_sections_and_listing_blocks():
    text = "= Doc\npre\n== Setup\n----\n== nope\n----\nafter"
    title, sections, _ = parse_note(text, "a.adoc")
    assert title == "Doc"
    assert [(lvl, h) for lvl, h, _ in sections] == [(1, "Doc"), (2, "Setup")]


def test_links_between_formats(make_index, notes):
    write(notes / "a.md", "# A\n[t](notes/log.txt) and [[Other]] and [[org note]]")
    write(notes / "notes" / "log.txt", "plain")
    write(notes / "Other.org", "* Other")
    write(notes / "org note.org", "* O")
    idx = make_index(extensions=("md", "txt", "org"))
    idx.sync()
    out, unresolved, _ = idx.links_for("a.md")
    assert sorted(out) == ["Other.org", "notes/log.txt", "org note.org"] and unresolved == []
    assert idx.links_for("notes/log.txt")[2] == ["a.md"]


def test_dotted_note_names_not_mangled(make_index, notes):
    write(notes / "release 1.2.md", "# R")
    write(notes / "x.md", "[[release 1.2]]")
    idx = make_index()
    idx.sync()
    assert idx.links_for("x.md")[0] == ["release 1.2.md"]
