"""Text extraction for non-plain-text note formats (standard library only)."""

import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_XML_BYTES = 64 * 1024 * 1024  # refuse zip bombs


class ExtractError(OSError):
    """A file could not be parsed (corrupt, encrypted, unsupported), as opposed to a transient I/O error."""


def _read_xml(zf: zipfile.ZipFile, name: str, required: bool):
    try:
        info = zf.getinfo(name)
    except KeyError:
        if required:
            raise ExtractError(f"not a valid .docx (missing {name})")
        return None
    if info.file_size > MAX_XML_BYTES:
        raise ExtractError(f"{name} is too large ({info.file_size} bytes)")
    return ET.fromstring(zf.read(info))


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _style_level_resolver(styles_root):
    """Return level_of(style_id) -> heading level 1..9 or None, following basedOn chains."""
    info = {}
    if styles_root is not None:
        for st in styles_root.iter(W + "style"):
            name = st.find(W + "name")
            based = st.find(W + "basedOn")
            outline = st.find(f"{W}pPr/{W}outlineLvl")
            info[st.get(W + "styleId")] = (
                (name.get(W + "val") if name is not None else "").lower(),
                based.get(W + "val") if based is not None else None,
                _int(outline.get(W + "val")) if outline is not None else None,
            )

    def level_of(style_id):
        if style_id is None:
            return None
        if style_id not in info:  # no styles.xml entry: fall back to the conventional ids
            m = re.fullmatch(r"Heading(\d)", style_id)
            return int(m.group(1)) if m else None
        for _ in range(8):
            if style_id not in info:
                return None
            name, based, outline = info[style_id]
            m = re.fullmatch(r"heading (\d)", name)  # built-in names are stored in English
            if m:
                return int(m.group(1))
            if name == "title":
                return 1
            if outline is not None:
                return outline + 1 if 0 <= outline < 9 else None
            style_id = based
        return None

    return level_of


def _para_text(p) -> str:
    out = []
    for child in p:
        if child.tag == W + "pPr":  # paragraph properties hold tab *stops*, not text
            continue
        for el in child.iter():
            if el.tag == W + "t":
                out.append(el.text or "")
            elif el.tag == W + "tab":
                out.append("\t")
            elif el.tag in (W + "br", W + "cr"):
                out.append(" ")
            elif el.tag == W + "noBreakHyphen":
                out.append("-")
    return "".join(out)


def _collect_blocks(parent, level_of, blocks) -> None:
    for el in parent:
        if el.tag == W + "p":
            text = _para_text(el).strip()
            if not text:
                continue
            ppr = el.find(W + "pPr")
            level, ilvl = None, None
            if ppr is not None:
                style = ppr.find(W + "pStyle")
                outline = ppr.find(W + "outlineLvl")
                if outline is not None:
                    o = _int(outline.get(W + "val"))
                    level = o + 1 if o is not None and 0 <= o < 9 else None
                else:
                    level = level_of(style.get(W + "val") if style is not None else None)
                if ppr.find(W + "numPr") is not None:
                    lv = ppr.find(f"{W}numPr/{W}ilvl")
                    ilvl = _int(lv.get(W + "val")) if lv is not None else 0
            if level:
                blocks.append(("h", "#" * min(level, 6) + " " + " ".join(text.split())))
                continue
            if re.match(r"#{1,6}\s", text):  # keep body text from being read as a heading
                text = "\\" + text
            if ilvl is not None:
                blocks.append(("li", "  " * (ilvl or 0) + "- " + text))
            else:
                blocks.append(("p", text))
        elif el.tag == W + "tbl":
            rows = []
            for tr in el.iter(W + "tr"):
                cells = []
                for tc in tr.findall(W + "tc"):
                    parts = (" ".join(_para_text(p).split()) for p in tc.iter(W + "p"))
                    cells.append(" ".join(x for x in parts if x))
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                blocks.append(("tbl", "\n".join(rows)))
        elif el.tag == W + "sdt":  # content controls wrap ordinary blocks
            content = el.find(W + "sdtContent")
            if content is not None:
                _collect_blocks(content, level_of, blocks)


def docx_to_markdown(path: Path) -> str:
    """Convert a .docx to Markdown-ish text: Word headings become '#' headings, list items '- ',
    tables 'a | b' rows. Images, headers/footers, footnotes and comments are not extracted."""
    try:
        with zipfile.ZipFile(path) as zf:
            doc = _read_xml(zf, "word/document.xml", required=True)
            styles = _read_xml(zf, "word/styles.xml", required=False)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError,
            ET.ParseError) as e:
        raise ExtractError(f"cannot read .docx: {e}") from e
    body = doc.find(W + "body")
    blocks: list[tuple[str, str]] = []
    if body is not None:
        _collect_blocks(body, _style_level_resolver(styles), blocks)
    out = []
    for i, (kind, text) in enumerate(blocks):
        if i:
            out.append("\n" if kind == "li" and blocks[i - 1][0] == "li" else "\n\n")
        out.append(text)
    return "".join(out)
