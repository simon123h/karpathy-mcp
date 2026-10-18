"""Note formats: supported extensions, section splitting, link extraction, section lookup."""

import argparse
import posixpath
import re
from pathlib import Path
from urllib.parse import unquote

from .extract import docx_to_markdown

SUPPORTED_EXTENSIONS = (
    "md",
    "markdown",
    "mdx",
    "txt",
    "text",
    "rst",
    "org",
    "adoc",
    "asciidoc",
    "docx",
)
DEFAULT_EXTENSIONS = SUPPORTED_EXTENSIONS

FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$")
ORG_FENCE_RE = re.compile(r"^\s*#\+(?:begin|end)_", re.IGNORECASE)
ORG_HEADING_RE = re.compile(r"^(\*{1,6})\s+(.+?)\s*$")
ADOC_FENCE_RE = re.compile(r"^(?:-{4,}|\.{4,})\s*$")
ADOC_HEADING_RE = re.compile(r"^(={1,6})\s+(.+?)\s*$")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
MDLINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def split_ext(name: str) -> tuple[str, str | None]:
    """Split off a supported note extension; (name, None) if it has none."""
    dot = name.rfind(".")
    if dot > 0 and name[dot + 1 :].lower() in SUPPORTED_EXTENSIONS:
        return name[:dot], name[dot + 1 :].lower()
    return name, None


def strip_ext(name: str) -> str:
    return split_ext(name)[0]


def parse_extensions(value: str) -> tuple[str, ...]:
    """Parse a comma-separated extension list such as 'md,.txt' (argparse type)."""
    exts = tuple(
        dict.fromkeys(e.strip().lstrip(".").lower() for e in value.split(",") if e.strip())
    )
    bad = [e for e in exts if e not in SUPPORTED_EXTENSIONS]
    if bad or not exts:
        raise argparse.ArgumentTypeError(
            f"unsupported extension(s) {bad or value!r}; "
            f"supported: {', '.join(SUPPORTED_EXTENSIONS)}"
        )
    return exts


# Per-format parsing: heading/fence patterns; markdown-family notes also get frontmatter and links.
# Formats without a heading pattern (txt, rst) are indexed as one section.
_MARKDOWN = dict(heading=HEADING_RE, fence=FENCE_RE, markdown=True, clean=lambda h: h)
_ORG = dict(
    heading=ORG_HEADING_RE,
    fence=ORG_FENCE_RE,
    markdown=False,
    clean=lambda h: re.sub(r"\s+:[\w@#%:]+:$", "", h),
)
_ADOC = dict(heading=ADOC_HEADING_RE, fence=ADOC_FENCE_RE, markdown=False, clean=lambda h: h)
# .docx is converted to Markdown by extract.py first; its text is never treated as
# frontmatter, fences or links
_DOCX = dict(heading=HEADING_RE, fence=re.compile(r"(?!)"), markdown=False, clean=lambda h: h)
_FORMATS = {
    "docx": _DOCX,
    "md": _MARKDOWN,
    "markdown": _MARKDOWN,
    "mdx": _MARKDOWN,
    "org": _ORG,
    "adoc": _ADOC,
    "asciidoc": _ADOC,
}


def read_note_file(path: Path) -> str:
    """Read a note's text; binary formats are converted.

    Raises OSError (ExtractError if the file cannot be parsed).
    """
    if path.suffix.lower() == ".docx":
        return docx_to_markdown(path)
    return path.read_text(encoding="utf-8", errors="replace")


def parse_note(text: str, rel_path: str):
    """Split a note into heading sections and collect its outgoing links.

    The format is chosen by file extension. Returns (title, sections, links); sections is a
    list of (level, heading, body) where level 0 is the content before the first heading
    (incl. frontmatter) and body includes the heading line itself.
    """
    spec = _FORMATS.get(split_ext(rel_path)[1])
    if spec is None:  # plain text: a single section, no links
        return Path(rel_path).stem, ([(0, "", text)] if text.strip() else []), set()
    lines = text.splitlines()
    sections: list[tuple[int, str, str]] = []
    links: set[str] = set()
    cur_level, cur_heading, cur_lines = 0, "", []

    start = 0
    if spec["markdown"] and lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                cur_lines = lines[: j + 1]
                start = j + 1
                break

    in_fence = False
    src_dir = posixpath.dirname(rel_path)
    for line in lines[start:]:
        if spec["fence"].match(line):
            in_fence = not in_fence
        m = None if in_fence else spec["heading"].match(line)
        if m:
            if any(ln.strip() for ln in cur_lines):
                sections.append((cur_level, cur_heading, "\n".join(cur_lines)))
            cur_level, cur_heading, cur_lines = len(m.group(1)), spec["clean"](m.group(2)), [line]
            continue
        cur_lines.append(line)
        if in_fence or not spec["markdown"]:
            continue
        for w in WIKILINK_RE.findall(line):
            links.add(strip_ext(w.strip().lower()))
        for target in MDLINK_RE.findall(line):
            target = unquote(target.split("#", 1)[0])
            if split_ext(target)[1] is None or "://" in target:
                continue
            norm = posixpath.normpath(posixpath.join(src_dir, target))
            if not norm.startswith(".."):
                links.add(strip_ext(norm.lower()))
    if any(ln.strip() for ln in cur_lines):
        sections.append((cur_level, cur_heading, "\n".join(cur_lines)))

    title = next((h for lvl, h, _ in sections if lvl == 1), Path(rel_path).stem)
    return title, sections, links


def extract_section(sections, name: str) -> list[str]:
    """Return the text of every section whose heading matches name (with its subsections)."""
    wanted = {name.strip().lower()}
    if " > " in name:
        wanted.add(name.split(" > ", 1)[1].strip().lower())
    out = []
    for i, (level, heading, body) in enumerate(sections):
        if level == 0 or heading.lower() not in wanted:
            continue
        parts = [body]
        for lvl2, _, body2 in sections[i + 1 :]:
            if lvl2 <= level:
                break
            parts.append(body2)
        out.append("\n".join(parts))
    return out


def format_outline(sections) -> str:
    """Heading outline with per-section sizes, e.g. '## Deployment (1.2k chars)'."""
    rows = []
    for lvl, heading, body in sections:
        if lvl:
            size = f"{len(body) / 1000:.1f}k" if len(body) >= 1000 else str(len(body))
            rows.append(f"{'#' * lvl} {heading} ({size} chars)")
    return "\n".join(rows)
