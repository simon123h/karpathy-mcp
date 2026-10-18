"""CLI entry point and MCP tool definitions."""

import argparse
import hashlib
import logging
import posixpath
import re
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .index import NotesIndex
from .parsing import (
    DEFAULT_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    extract_section,
    format_outline,
    parse_extensions,
    parse_note,
    read_note_file,
)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Markdown notes MCP server (SQLite FTS5)")
    p.add_argument("--notes-dir", required=True, type=Path, help="Root directory of notes")
    p.add_argument("--name", default="markdown-notes", help="MCP server display name")
    p.add_argument(
        "--extensions",
        type=parse_extensions,
        default=DEFAULT_EXTENSIONS,
        help="Comma-separated file extensions to index (default: all supported: "
        f"{', '.join(SUPPORTED_EXTENSIONS)}). docx is converted to text with Word headings "
        "as sections; only markdown-family files contribute [[wikilinks]]/links; "
        "txt and rst are indexed as a single section.",
    )
    p.add_argument(
        "--description",
        default="",
        help="What these notes contain; shown to the agent in the server instructions",
    )
    p.add_argument("--cache-dir", type=Path, default=None, help="Location for the SQLite index")
    p.add_argument(
        "--sync-interval",
        type=float,
        default=60.0,
        help="Max seconds between automatic rescans, checked on search/list (default: 60)",
    )
    return p.parse_args(argv)


def default_db_path(name: str, notes_dir: Path) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
    if not safe:
        safe = hashlib.sha1(str(notes_dir).encode()).hexdigest()[:16]
    return Path.home() / ".cache" / "mcp-markdown" / f"{safe}.db"


def build_instructions(name: str, description: str) -> str:
    about = f" {description.strip()}" if description.strip() else ""
    return (
        f"Read-only knowledge base '{name}': a directory of notes "
        f"(Markdown and similar text files).{about}\n\n"
        "How to use it:\n"
        "- Start with search_notes. It matches sections (split at headings); each hit is "
        "'path — Note title > Section heading' plus a snippet. Plain words work; FTS5 syntax "
        '("phrases", prefix*, OR, NOT) is optional. Matching is keyword-based, so retry '
        "with synonyms or related terms if nothing relevant comes back.\n"
        "- Read only what you need: get_outline(path) lists a note's headings with section sizes; "
        'read_note(path, section="Section heading") returns just that section (and its '
        "subsections); omit section for the whole note.\n"
        "- Notes link to each other with [[wikilinks]] and relative .md links. Use get_links(path) "
        "to see what a note links to and which notes link back to it, and follow the graph for "
        "related context.\n"
        "- list_notes(prefix) shows the file tree by path prefix. Paths are always relative to "
        "the notes root, e.g. 'projects/alpha.md'.\n"
        "- The index refreshes in the background roughly once a minute (a search never waits "
        "for it, so a very recent edit may show up one query later); call sync_notes to force "
        "a refresh."
    )


def build_server(args) -> FastMCP:
    notes_dir = args.notes_dir.expanduser().resolve()
    if not notes_dir.is_dir():
        raise SystemExit(f"--notes-dir is not a directory: {notes_dir}")
    db_path = (
        args.cache_dir.expanduser() if args.cache_dir else default_db_path(args.name, notes_dir)
    )
    if db_path.suffix != ".db":  # treat as a directory
        db_path = db_path / f"{default_db_path(args.name, notes_dir).stem}.db"
    index = NotesIndex(notes_dir, db_path, args.sync_interval, extensions=args.extensions)
    index.maybe_sync()  # initial index runs in the background so the server starts immediately

    def with_note(text: str) -> str:
        if index.initial_sync_pending:
            return (
                text + "\n\n(Note: the initial indexing is still running, so results may be "
                "incomplete. Retry shortly.)"
            )
        return text

    mcp = FastMCP(args.name, instructions=build_instructions(args.name, args.description))

    @mcp.tool()
    def search_notes(query: str, limit: int = 10) -> str:
        """Full-text search over note sections, best matches first. Returns 'path — Note > Section'
        and a snippet per hit. Use read_note with that path (and section) to read more."""
        rows = index.search(query, max(1, min(limit, 100)))
        if not rows:
            return with_note(
                "No results. Try different or fewer keywords, synonyms, or a prefix like term*."
            )
        return with_note("\n\n".join(f"{p} — {t}\n{s}" for p, t, s in rows))

    def load_note(path: str) -> tuple[str, str]:
        """Return (normalised path, text), served from the index when possible.

        Raises ValueError for paths outside the notes root and OSError if unreadable.
        """
        rel = posixpath.normpath(path.replace("\\", "/"))
        full = index.resolve(rel)
        text = index.get_text(rel)
        if text is None:  # not indexed yet (e.g. created since the last sync): read from disk
            text = read_note_file(full)
        return rel, text

    @mcp.tool()
    def read_note(path: str, section: str = "") -> str:
        """Read a note by its path relative to the notes root. Pass section (a heading text,
        e.g. from a search hit) to read only that section and its subsections. Content comes
        from the index, so it can be up to the sync interval out of date."""
        try:
            rel, text = load_note(path)
        except ValueError as e:
            return f"Error: {e}"
        except OSError as e:
            return f"Error: cannot read {path}: {e.strerror or e}"
        if not section.strip():
            return text
        _, sections, _ = parse_note(text, rel)
        found = extract_section(sections, section)
        if found:
            return "\n\n".join(found)
        outline = format_outline(sections)
        return (
            f"Error: no section '{section}' in {path}. Sections:\n{outline}"
            if outline
            else f"Error: {path} has no headings."
        )

    @mcp.tool()
    def get_outline(path: str) -> str:
        """Show a note's heading structure with the size of each section. Use it to pick which
        section to read with read_note(path, section=...) instead of loading a long note whole."""
        try:
            rel, text = load_note(path)
        except ValueError as e:
            return f"Error: {e}"
        except OSError as e:
            return f"Error: cannot read {path}: {e.strerror or e}"
        _, sections, _ = parse_note(text, rel)
        return format_outline(sections) or "No headings."

    @mcp.tool()
    def list_notes(prefix: str = "") -> str:
        """List indexed notes (path — title), optionally filtered by a relative path prefix."""
        rows = index.list(prefix)
        return with_note("\n".join(f"{p} — {t}" for p, t in rows) or "No notes.")

    @mcp.tool()
    def get_links(path: str) -> str:
        """Show which notes a note links to (via [[wikilinks]] or relative .md links) and
        which notes link back to it (backlinks). Use it to explore related notes."""
        try:
            index.resolve(path)
        except ValueError as e:
            return f"Error: {e}"
        result = index.links_for(Path(path).as_posix())
        if result is None:
            return with_note(f"Error: {path} is not an indexed note. Use list_notes to see paths.")
        outgoing, unresolved, backlinks = result
        out = ["Links to:" + ("".join(f"\n- {p}" for p in outgoing) or " none")]
        if unresolved:
            out.append("Unresolved links (no matching note): " + ", ".join(unresolved))
        out.append("Linked from:" + ("".join(f"\n- {p}" for p in backlinks) or " none"))
        return with_note("\n".join(out))

    @mcp.tool()
    def sync_notes() -> str:
        """Re-scan the notes directory now and update the index with changes."""
        s = index.sync()
        return (
            f"Synced: {s['added']} added, {s['updated']} updated, "
            f"{s['removed']} removed ({s['total']} total)."
        )

    return mcp


def main():
    # stdout carries the JSON-RPC stream: log to stderr only, never print()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
    build_server(parse_args()).run()
