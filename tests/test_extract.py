import zipfile
from xml.sax.saxutils import escape

import pytest

import server
from extract import ExtractError, docx_to_markdown
from server import NotesIndex, parse_note

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def para(text, style=None, ppr="", runs=None):
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    body = runs if runs is not None else f"<w:r><w:t>{escape(text)}</w:t></w:r>"
    return f"<w:p><w:pPr>{style_xml}{ppr}</w:pPr>{body}</w:p>"


def make_docx(path, body, styles=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f"<w:document {NS}><w:body>{body}</w:body></w:document>")
        if styles is not None:
            z.writestr("word/styles.xml", f"<w:styles {NS}>{styles}</w:styles>")
    return path


STYLES = (
    '<w:style w:styleId="Kop1"><w:name w:val="heading 1"/></w:style>'  # localized id, English name
    '<w:style w:styleId="Kop2"><w:name w:val="Heading 2"/></w:style>'
    '<w:style w:styleId="Custom"><w:name w:val="My Section"/><w:basedOn w:val="Kop2"/></w:style>'
    '<w:style w:styleId="Outl"><w:name w:val="Outlined"/><w:pPr><w:outlineLvl w:val="2"/></w:pPr></w:style>'
    '<w:style w:styleId="Title"><w:name w:val="Title"/></w:style>'
)


def test_headings_lists_tables(tmp_path):
    body = (
        para("Doc Title", "Title")
        + para("intro text")
        + para("Setup", "Kop1")
        + para("a step", ppr='<w:numPr><w:ilvl w:val="1"/><w:numId w:val="1"/></w:numPr>')
        + para("Custom section", "Custom")
        + para("Deep", "Outl")
        + "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>k</w:t></w:r></w:p></w:tc>"
          "<w:tc><w:p><w:r><w:t>v</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    md = docx_to_markdown(make_docx(tmp_path / "a.docx", body, STYLES))
    assert md == ("# Doc Title\n\nintro text\n\n# Setup\n\n  - a step\n\n"
                  "## Custom section\n\n### Deep\n\nk | v")


def test_heading_ids_without_styles_xml(tmp_path):
    md = docx_to_markdown(make_docx(tmp_path / "a.docx", para("Hi", "Heading2")))
    assert md == "## Hi"


def test_text_details(tmp_path):
    runs = ('<w:r><w:t>a</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>b</w:t></w:r>'
            '<w:del><w:r><w:delText>gone</w:delText></w:r></w:del>'
            '<w:ins><w:r><w:t>new</w:t></w:r></w:ins><w:r><w:br/></w:r><w:r><w:t>c</w:t></w:r>')
    ppr = '<w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs>'  # tab stop definition, not text
    body = para("", ppr=ppr, runs=runs) + para("# not a heading") + para("   ")
    assert docx_to_markdown(make_docx(tmp_path / "a.docx", body)) == "a\tbnew c\n\n\\# not a heading"


def test_content_controls_are_descended(tmp_path):
    body = f"<w:sdt><w:sdtContent>{para('inside')}</w:sdtContent></w:sdt>"
    assert docx_to_markdown(make_docx(tmp_path / "a.docx", body)) == "inside"


def test_corrupt_files_raise_extract_error(tmp_path):
    (tmp_path / "bad.docx").write_bytes(b"not a zip")
    with pytest.raises(ExtractError):
        docx_to_markdown(tmp_path / "bad.docx")
    with zipfile.ZipFile(tmp_path / "empty.docx", "w") as z:
        z.writestr("other.xml", "<x/>")
    with pytest.raises(ExtractError):
        docx_to_markdown(tmp_path / "empty.docx")
    with zipfile.ZipFile(tmp_path / "xml.docx", "w") as z:
        z.writestr("word/document.xml", "<w:document")
    with pytest.raises(ExtractError):
        docx_to_markdown(tmp_path / "xml.docx")


def test_missing_file_is_plain_oserror(tmp_path):
    with pytest.raises(OSError) as e:
        docx_to_markdown(tmp_path / "nope.docx")
    assert not isinstance(e.value, ExtractError)


@pytest.fixture
def index(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    return notes, lambda: NotesIndex(notes, tmp_path / "c.db", extensions=("md", "docx"))


def test_docx_indexed_with_sections(index):
    notes, make = index
    body = para("Handbook", "Kop1") + para("intro") + para("Deployment", "Kop2") + para("run the pipeline")
    (notes / "sub").mkdir()
    make_docx(notes / "sub" / "hb.docx", body, STYLES)
    idx = make()
    assert idx.sync()["total"] == 1
    assert [(p, t) for p, t, _ in idx.search("pipeline", 10)] == [("sub/hb.docx", "Handbook > Deployment")]
    assert idx.list("") == [("sub/hb.docx", "Handbook")]
    text = idx.get_text("sub/hb.docx")
    _, sections, _ = parse_note(text, "sub/hb.docx")
    assert [h for lvl, h, _ in sections if lvl] == ["Handbook", "Deployment"]


def test_docx_text_never_creates_links_or_fences(index):
    notes, make = index
    make_docx(notes / "a.docx", para("see [[b]] and [c](c.md)") + para("```") + para("# H"))
    idx = make()
    idx.sync()
    assert idx.links_for("a.docx") == ([], [], [])
    assert len(idx.search("H", 10)) == 1


def test_corrupt_docx_indexed_empty_and_not_retried(index, monkeypatch):
    notes, make = index
    (notes / "bad.docx").write_bytes(b"junk")
    idx = make()
    assert idx.sync() == {"added": 1, "updated": 0, "removed": 0, "total": 1}
    assert idx.list("") == [("bad.docx", "bad")]
    calls = []
    monkeypatch.setattr(server, "docx_to_markdown", lambda p: calls.append(p) or "")
    assert idx.sync()["updated"] == 0 and calls == []


def test_word_lock_files_skipped(index):
    notes, make = index
    make_docx(notes / "real.docx", para("x"))
    (notes / "~$real.docx").write_bytes(b"\x00lock")
    idx = make()
    assert idx.sync()["total"] == 1
