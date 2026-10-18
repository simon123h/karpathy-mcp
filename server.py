#!/usr/bin/env python3
"""Configurable Markdown knowledge base MCP server backed by SQLite FTS5."""

import argparse
import hashlib
import logging
import os
import posixpath
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote

from mcp.server.fastmcp import FastMCP

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("mcp-markdown")

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS file_meta (
    rel_path TEXT PRIMARY KEY,
    mtime REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    rel_path UNINDEXED,
    title,
    content,
    tokenize = 'unicode61 remove_diacritics 1'
);
-- Outgoing links, targets normalised (lowercase, no .md); resolved lazily at query time
CREATE TABLE IF NOT EXISTS links (
    src TEXT NOT NULL,
    target TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS links_src ON links (src);
CREATE INDEX IF NOT EXISTS links_target ON links (target);
"""

FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
MDLINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _strip_md(name: str) -> str:
    return name[:-3] if name.lower().endswith(".md") else name


def parse_note(text: str, rel_path: str):
    """Split a note into heading sections and collect its outgoing links.

    Returns (title, sections, links); sections is a list of (level, heading, body)
    where level 0 is the content before the first heading (incl. frontmatter) and
    body includes the heading line itself.
    """
    lines = text.splitlines()
    sections: list[tuple[int, str, str]] = []
    links: set[str] = set()
    cur_level, cur_heading, cur_lines = 0, "", []

    start = 0
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                cur_lines = lines[: j + 1]
                start = j + 1
                break

    in_fence = False
    src_dir = posixpath.dirname(rel_path)
    for line in lines[start:]:
        if FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            if any(l.strip() for l in cur_lines):
                sections.append((cur_level, cur_heading, "\n".join(cur_lines)))
            cur_level, cur_heading, cur_lines = len(m.group(1)), m.group(2), [line]
            continue
        cur_lines.append(line)
        if in_fence:
            continue
        for w in WIKILINK_RE.findall(line):
            links.add(_strip_md(w.strip().lower()))
        for target in MDLINK_RE.findall(line):
            target = unquote(target.split("#", 1)[0])
            if not target.lower().endswith(".md") or "://" in target:
                continue
            norm = posixpath.normpath(posixpath.join(src_dir, target))
            if not norm.startswith(".."):
                links.add(_strip_md(norm.lower()))
    if any(l.strip() for l in cur_lines):
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
        for lvl2, _, body2 in sections[i + 1:]:
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


class NotesIndex:
    def __init__(self, notes_dir: Path, db_path: Path, sync_interval: float = 60.0):
        self.root = notes_dir.resolve()
        self.sync_interval = sync_interval
        self.last_sync = 0.0
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("PRAGMA journal_mode = WAL;")
        self.db.execute("PRAGMA synchronous = NORMAL;")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            # Older index layout (whole-file rows, no links): rebuild from scratch
            for t in ("notes_fts", "file_meta", "links"):
                self.db.execute(f"DROP TABLE IF EXISTS {t}")
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.db.executescript(SCHEMA)

    def resolve(self, rel_path: str) -> Path:
        """Resolve a relative path, rejecting anything outside the notes root."""
        full = (self.root / rel_path).resolve()
        if full != self.root and self.root not in full.parents:
            raise ValueError(f"Path escapes notes directory: {rel_path}")
        return full

    def _scan(self) -> dict[str, float]:
        found: dict[str, float] = {}
        stack = [self.root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            if not entry.name.startswith("."):
                                stack.append(Path(entry.path))
                        elif entry.is_file() and entry.name.lower().endswith(".md"):
                            rel = Path(entry.path).relative_to(self.root).as_posix()
                            found[rel] = entry.stat().st_mtime
            except OSError as e:
                log.warning("Cannot scan %s: %s", current, e)
        return found

    def _drop(self, rel: str) -> None:
        self.db.execute("DELETE FROM notes_fts WHERE rel_path = ?", (rel,))
        self.db.execute("DELETE FROM links WHERE src = ?", (rel,))

    def sync(self) -> dict[str, int]:
        with self.lock:
            on_disk = self._scan()
            known = dict(self.db.execute("SELECT rel_path, mtime FROM file_meta"))
            added = updated = 0
            for rel, mtime in on_disk.items():
                if rel in known and known[rel] == mtime:
                    continue
                try:
                    text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    log.warning("Cannot read %s: %s", rel, e)
                    continue
                title, sections, links = parse_note(text, rel)
                self._drop(rel)
                for n, (level, heading, body) in enumerate(sections):
                    # first chunk carries the bare note title (used for listings)
                    chunk_title = title if n == 0 or not heading or heading == title \
                        else f"{title} > {heading}"
                    self.db.execute(
                        "INSERT INTO notes_fts (rel_path, title, content) VALUES (?, ?, ?)",
                        (rel, chunk_title, body),
                    )
                if not sections:  # empty file: keep it listed
                    self.db.execute(
                        "INSERT INTO notes_fts (rel_path, title, content) VALUES (?, ?, '')",
                        (rel, title),
                    )
                self.db.executemany("INSERT INTO links (src, target) VALUES (?, ?)",
                                    [(rel, t) for t in sorted(links)])
                self.db.execute(
                    "INSERT OR REPLACE INTO file_meta (rel_path, mtime) VALUES (?, ?)",
                    (rel, mtime),
                )
                if rel in known:
                    updated += 1
                else:
                    added += 1
            removed_paths = [r for r in known if r not in on_disk]
            for rel in removed_paths:
                self._drop(rel)
                self.db.execute("DELETE FROM file_meta WHERE rel_path = ?", (rel,))
            self.db.commit()
            stats = {"added": added, "updated": updated, "removed": len(removed_paths),
                     "total": len(on_disk)}
            self.last_sync = time.monotonic()
            log.info("sync: %s", stats)
            return stats

    def maybe_sync(self) -> None:
        """Resync if the last scan is older than sync_interval (metadata-only when nothing changed)."""
        if time.monotonic() - self.last_sync >= self.sync_interval:
            self.sync()

    def _match(self, fts_query: str, limit: int) -> list[tuple[str, str, str]]:
        with self.lock:
            return self.db.execute(
                "SELECT rel_path, title, snippet(notes_fts, 2, '**', '**', '…', 24) "
                "FROM notes_fts WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts, 0, 5.0, 1.0) LIMIT ?",
                (fts_query, limit),
            ).fetchall()

    def search(self, query: str, limit: int) -> list[tuple[str, str, str]]:
        """Search sections. Raw FTS5 syntax first; on a syntax error, retry with plain quoted terms."""
        self.maybe_sync()
        try:
            return self._match(query, limit)
        except sqlite3.OperationalError:
            terms = re.findall(r"\w+", query)
            if not terms:
                return []
            return self._match(" ".join(f'"{t}"' for t in terms), limit)

    def list(self, prefix: str) -> list[tuple[str, str]]:
        self.maybe_sync()
        with self.lock:
            return self.db.execute(
                "SELECT rel_path, title FROM notes_fts "
                "WHERE rowid IN (SELECT min(rowid) FROM notes_fts GROUP BY rel_path) "
                "AND rel_path LIKE ? ESCAPE '\\' ORDER BY rel_path",
                (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
            ).fetchall()

    def get_text(self, rel_path: str) -> str | None:
        """Note text rebuilt from the indexed sections (no disk access); None if not indexed.

        Line endings are normalised to \\n and a trailing newline is not preserved.
        """
        with self.lock:
            rows = self.db.execute(
                "SELECT content FROM notes_fts WHERE rel_path = ? ORDER BY rowid", (rel_path,)
            ).fetchall()
        return "\n".join(r[0] for r in rows) if rows else None

    def links_for(self, rel_path: str):
        """Return (outgoing, unresolved, backlinks) for an indexed note, or None if not indexed."""
        self.maybe_sync()
        with self.lock:
            paths = [r[0] for r in self.db.execute("SELECT rel_path FROM file_meta")]
            if rel_path not in paths:
                return None
            by_path = {_strip_md(p.lower()): p for p in paths}
            by_stem: dict[str, str] = {}
            for p in sorted(paths):
                by_stem.setdefault(posixpath.basename(_strip_md(p.lower())), p)

            def resolve(target: str):
                return by_path.get(target) or by_stem.get(target)

            outgoing, unresolved = [], []
            for (t,) in self.db.execute("SELECT target FROM links WHERE src = ? ORDER BY target", (rel_path,)):
                dest = resolve(t)
                if dest is None:
                    unresolved.append(t)
                elif dest != rel_path and dest not in outgoing:
                    outgoing.append(dest)
            me = _strip_md(rel_path.lower())
            keys = (me, posixpath.basename(me))
            backlinks = sorted({
                src for src, t in self.db.execute(
                    "SELECT src, target FROM links WHERE target IN (?, ?)", keys)
                if src != rel_path and resolve(t) == rel_path
            })
            return outgoing, unresolved, backlinks


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Markdown notes MCP server (SQLite FTS5)")
    p.add_argument("--notes-dir", required=True, type=Path, help="Root directory of Markdown notes")
    p.add_argument("--name", default="markdown-notes", help="MCP server display name")
    p.add_argument("--description", default="",
                   help="What these notes contain; shown to the agent in the server instructions")
    p.add_argument("--cache-dir", type=Path, default=None, help="Location for the SQLite index")
    p.add_argument("--sync-interval", type=float, default=60.0,
                   help="Max seconds between automatic rescans, checked on search/list (default: 60)")
    return p.parse_args(argv)


def default_db_path(name: str, notes_dir: Path) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
    if not safe:
        safe = hashlib.sha1(str(notes_dir).encode()).hexdigest()[:16]
    return Path.home() / ".cache" / "mcp-markdown" / f"{safe}.db"


def build_instructions(name: str, description: str) -> str:
    about = f" {description.strip()}" if description.strip() else ""
    return (
        f"Read-only knowledge base '{name}': a directory of Markdown notes.{about}\n\n"
        "How to use it:\n"
        "- Start with search_notes. It matches sections (split at headings); each hit is "
        "'path — Note title > Section heading' plus a snippet. Plain words work; FTS5 syntax "
        "(\"phrases\", prefix*, OR, NOT) is optional. Matching is keyword-based, so retry "
        "with synonyms or related terms if nothing relevant comes back.\n"
        "- Read only what you need: get_outline(path) lists a note's headings with section sizes; "
        "read_note(path, section=\"Section heading\") returns just that section (and its "
        "subsections); omit section for the whole note.\n"
        "- Notes link to each other with [[wikilinks]] and relative .md links. Use get_links(path) "
        "to see what a note links to and which notes link back to it, and follow the graph for "
        "related context.\n"
        "- list_notes(prefix) shows the file tree by path prefix. Paths are always relative to "
        "the notes root, e.g. 'projects/alpha.md'.\n"
        "- The index refreshes automatically about once a minute; call sync_notes to force it."
    )


def build_server(args) -> FastMCP:
    notes_dir = args.notes_dir.expanduser().resolve()
    if not notes_dir.is_dir():
        raise SystemExit(f"--notes-dir is not a directory: {notes_dir}")
    db_path = args.cache_dir.expanduser() if args.cache_dir else default_db_path(args.name, notes_dir)
    if db_path.suffix != ".db":  # treat as a directory
        db_path = db_path / f"{default_db_path(args.name, notes_dir).stem}.db"
    index = NotesIndex(notes_dir, db_path, args.sync_interval)
    index.sync()

    mcp = FastMCP(args.name, instructions=build_instructions(args.name, args.description))

    @mcp.tool()
    def search_notes(query: str, limit: int = 10) -> str:
        """Full-text search over note sections, best matches first. Returns 'path — Note > Section'
        and a snippet per hit. Use read_note with that path (and section) to read more."""
        rows = index.search(query, max(1, min(limit, 100)))
        if not rows:
            return "No results. Try different or fewer keywords, synonyms, or a prefix like term*."
        return "\n\n".join(f"{p} — {t}\n{s}" for p, t, s in rows)

    def load_note(path: str) -> tuple[str, str]:
        """Return (normalised path, text), served from the index when possible.

        Raises ValueError for paths outside the notes root and OSError if unreadable.
        """
        rel = posixpath.normpath(path.replace("\\", "/"))
        full = index.resolve(rel)
        text = index.get_text(rel)
        if text is None:  # not indexed yet (e.g. created since the last sync): read from disk
            text = full.read_text(encoding="utf-8", errors="replace")
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
            return f"Error: cannot read {path}: {e.strerror}"
        if not section.strip():
            return text
        _, sections, _ = parse_note(text, rel)
        found = extract_section(sections, section)
        if found:
            return "\n\n".join(found)
        outline = format_outline(sections)
        return (f"Error: no section '{section}' in {path}. Sections:\n{outline}"
                if outline else f"Error: {path} has no headings.")

    @mcp.tool()
    def get_outline(path: str) -> str:
        """Show a note's heading structure with the size of each section. Use it to pick which
        section to read with read_note(path, section=...) instead of loading a long note whole."""
        try:
            rel, text = load_note(path)
        except ValueError as e:
            return f"Error: {e}"
        except OSError as e:
            return f"Error: cannot read {path}: {e.strerror}"
        _, sections, _ = parse_note(text, rel)
        return format_outline(sections) or "No headings."

    @mcp.tool()
    def list_notes(prefix: str = "") -> str:
        """List indexed notes (path — title), optionally filtered by a relative path prefix."""
        rows = index.list(prefix)
        return "\n".join(f"{p} — {t}" for p, t in rows) or "No notes."

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
            return f"Error: {path} is not an indexed note. Use list_notes to see paths."
        outgoing, unresolved, backlinks = result
        out = ["Links to:" + ("".join(f"\n- {p}" for p in outgoing) or " none")]
        if unresolved:
            out.append("Unresolved links (no matching note): " + ", ".join(unresolved))
        out.append("Linked from:" + ("".join(f"\n- {p}" for p in backlinks) or " none"))
        return "\n".join(out)

    @mcp.tool()
    def sync_notes() -> str:
        """Re-scan the notes directory now and update the index with changes."""
        s = index.sync()
        return (f"Synced: {s['added']} added, {s['updated']} updated, "
                f"{s['removed']} removed ({s['total']} total).")

    return mcp


def main():
    build_server(parse_args()).run()


if __name__ == "__main__":
    main()
