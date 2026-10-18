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

SUPPORTED_EXTENSIONS = ("md", "markdown", "mdx", "txt", "text", "rst", "org", "adoc", "asciidoc")
DEFAULT_EXTENSIONS = ("md", "markdown", "mdx")

FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$")
ORG_FENCE_RE = re.compile(r"^\s*#\+(?:begin|end)_", re.IGNORECASE)
ORG_HEADING_RE = re.compile(r"^(\*{1,6})\s+(.+?)\s*$")
ADOC_FENCE_RE = re.compile(r"^(?:-{4,}|\.{4,})\s*$")
ADOC_HEADING_RE = re.compile(r"^(={1,6})\s+(.+?)\s*$")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
MDLINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _split_ext(name: str) -> tuple[str, str | None]:
    """Split off a supported note extension; (name, None) if it has none."""
    dot = name.rfind(".")
    if dot > 0 and name[dot + 1:].lower() in SUPPORTED_EXTENSIONS:
        return name[:dot], name[dot + 1:].lower()
    return name, None


def _strip_ext(name: str) -> str:
    return _split_ext(name)[0]


def parse_extensions(value: str) -> tuple[str, ...]:
    """Parse a comma-separated extension list such as 'md,.txt' (argparse type)."""
    exts = tuple(dict.fromkeys(e.strip().lstrip(".").lower() for e in value.split(",") if e.strip()))
    bad = [e for e in exts if e not in SUPPORTED_EXTENSIONS]
    if bad or not exts:
        raise argparse.ArgumentTypeError(
            f"unsupported extension(s) {bad or value!r}; supported: {', '.join(SUPPORTED_EXTENSIONS)}")
    return exts


# Per-format parsing: heading/fence patterns; markdown-family notes also get frontmatter and links.
# Formats without a heading pattern (txt, rst) are indexed as one section.
_MARKDOWN = dict(heading=HEADING_RE, fence=FENCE_RE, markdown=True, clean=lambda h: h)
_ORG = dict(heading=ORG_HEADING_RE, fence=ORG_FENCE_RE, markdown=False,
            clean=lambda h: re.sub(r"\s+:[\w@#%:]+:$", "", h))
_ADOC = dict(heading=ADOC_HEADING_RE, fence=ADOC_FENCE_RE, markdown=False, clean=lambda h: h)
_FORMATS = {"md": _MARKDOWN, "markdown": _MARKDOWN, "mdx": _MARKDOWN,
            "org": _ORG, "adoc": _ADOC, "asciidoc": _ADOC}


def parse_note(text: str, rel_path: str):
    """Split a note into heading sections and collect its outgoing links.

    The format is chosen by file extension. Returns (title, sections, links); sections is a list of (level, heading, body)
    where level 0 is the content before the first heading (incl. frontmatter) and
    body includes the heading line itself.
    """
    spec = _FORMATS.get(_split_ext(rel_path)[1])
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
            if any(l.strip() for l in cur_lines):
                sections.append((cur_level, cur_heading, "\n".join(cur_lines)))
            cur_level, cur_heading, cur_lines = len(m.group(1)), spec["clean"](m.group(2)), [line]
            continue
        cur_lines.append(line)
        if in_fence or not spec["markdown"]:
            continue
        for w in WIKILINK_RE.findall(line):
            links.add(_strip_ext(w.strip().lower()))
        for target in MDLINK_RE.findall(line):
            target = unquote(target.split("#", 1)[0])
            if _split_ext(target)[1] is None or "://" in target:
                continue
            norm = posixpath.normpath(posixpath.join(src_dir, target))
            if not norm.startswith(".."):
                links.add(_strip_ext(norm.lower()))
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
    def __init__(self, notes_dir: Path, db_path: Path, sync_interval: float = 60.0,
                 startup_wait: float = 10.0, extensions: tuple[str, ...] = DEFAULT_EXTENSIONS):
        self.root = notes_dir.resolve()
        self.extensions = frozenset(extensions)
        self.sync_interval = sync_interval
        self.startup_wait = startup_wait
        self.last_sync = float("-inf")  # time.monotonic() of the last finished sync attempt
        self.ready = threading.Event()  # set once the first sync attempt has finished
        self.sync_lock = threading.Lock()  # held while a sync runs; keeps syncs single-flight
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.lock = threading.Lock()  # guards all database access
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
                        elif entry.is_file() and os.path.splitext(entry.name)[1][1:].lower() in self.extensions:
                            rel = Path(entry.path).relative_to(self.root).as_posix()
                            found[rel] = entry.stat().st_mtime
            except OSError as e:
                log.warning("Cannot scan %s: %s", current, e)
        return found

    def _drop(self, rel: str) -> None:
        self.db.execute("DELETE FROM notes_fts WHERE rel_path = ?", (rel,))
        self.db.execute("DELETE FROM links WHERE src = ?", (rel,))

    def sync(self) -> dict[str, int]:
        """Rescan now and wait for it (waits for a running background sync first)."""
        with self.sync_lock:
            return self._sync_locked()

    def _sync_locked(self) -> dict[str, int]:
        """Directory walk and file reads run without the DB lock so searches are never blocked by slow I/O."""
        t0 = time.monotonic()
        try:
            on_disk = self._scan()
            with self.lock:
                known = dict(self.db.execute("SELECT rel_path, mtime FROM file_meta"))
            loaded = []
            for rel, mtime in on_disk.items():
                if rel in known and known[rel] == mtime:
                    continue
                try:
                    text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    log.warning("Cannot read %s: %s", rel, e)
                    continue
                loaded.append((rel, mtime, *parse_note(text, rel)))
            removed_paths = [r for r in known if r not in on_disk]
            with self.lock:
                for rel, mtime, title, sections, links in loaded:
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
                for rel in removed_paths:
                    self._drop(rel)
                    self.db.execute("DELETE FROM file_meta WHERE rel_path = ?", (rel,))
                self.db.commit()
            stats = {"added": sum(1 for x in loaded if x[0] not in known),
                     "updated": sum(1 for x in loaded if x[0] in known),
                     "removed": len(removed_paths), "total": len(on_disk)}
            log.info("sync: %s in %.2fs", stats, time.monotonic() - t0)
            return stats
        finally:
            self.last_sync = time.monotonic()  # also after a failure, so errors don't retrigger every search
            self.ready.set()

    def _background_sync(self) -> None:
        try:
            self._sync_locked()
        except Exception:
            log.exception("background sync failed")
        finally:
            self.sync_lock.release()

    def maybe_sync(self) -> None:
        """If the index is older than sync_interval, start a background rescan (never blocks the caller)."""
        if time.monotonic() - self.last_sync < self.sync_interval:
            return
        if self.sync_lock.acquire(blocking=False):  # released by _background_sync
            threading.Thread(target=self._background_sync, daemon=True).start()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Wait until no sync is running (mainly for tests)."""
        if self.sync_lock.acquire(timeout=timeout):
            self.sync_lock.release()
            return True
        return False

    @property
    def initial_sync_pending(self) -> bool:
        return not self.ready.is_set()

    def _prepare(self) -> None:
        """Kick off a stale-index refresh; before the first sync has finished, wait for it (bounded)."""
        self.maybe_sync()
        if not self.ready.is_set():
            self.ready.wait(self.startup_wait)

    def _match(self, fts_query: str, limit: int) -> list[tuple[str, str, str]]:
        with self.lock:
            return self.db.execute(
                "SELECT rel_path, title, snippet(notes_fts, 2, '**', '**', '…', 24) "
                "FROM notes_fts WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts, 0, 5.0, 1.0) LIMIT ?",
                (fts_query, limit),
            ).fetchall()

    def search(self, query: str, limit: int) -> list[tuple[str, str, str]]:
        """Search sections. Raw FTS5 syntax first; on a syntax error, retry with plain quoted terms."""
        self._prepare()
        try:
            return self._match(query, limit)
        except sqlite3.OperationalError:
            terms = re.findall(r"\w+", query)
            if not terms:
                return []
            return self._match(" ".join(f'"{t}"' for t in terms), limit)

    def list(self, prefix: str) -> list[tuple[str, str]]:
        self._prepare()
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
        self._prepare()
        with self.lock:
            paths = [r[0] for r in self.db.execute("SELECT rel_path FROM file_meta")]
            if rel_path not in paths:
                return None
            by_path: dict[str, str] = {}
            by_stem: dict[str, str] = {}
            for p in sorted(paths):  # on a name clash (a.md vs a.txt) the first path wins
                by_path.setdefault(_strip_ext(p.lower()), p)
                by_stem.setdefault(posixpath.basename(_strip_ext(p.lower())), p)

            def resolve(target: str):
                return by_path.get(target) or by_stem.get(target)

            outgoing, unresolved = [], []
            for (t,) in self.db.execute("SELECT target FROM links WHERE src = ? ORDER BY target", (rel_path,)):
                dest = resolve(t)
                if dest is None:
                    unresolved.append(t)
                elif dest != rel_path and dest not in outgoing:
                    outgoing.append(dest)
            me = _strip_ext(rel_path.lower())
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
    p.add_argument("--extensions", type=parse_extensions, default=DEFAULT_EXTENSIONS,
                   help="Comma-separated file extensions to index (default: md,markdown,mdx). "
                        f"Supported: {', '.join(SUPPORTED_EXTENSIONS)}. Only markdown-family files "
                        "contribute [[wikilinks]]/links; txt and rst are indexed as a single section.")
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
        f"Read-only knowledge base '{name}': a directory of notes (Markdown and similar text files).{about}\n\n"
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
        "- The index refreshes in the background roughly once a minute (a search never waits for it, "
        "so a very recent edit may show up one query later); call sync_notes to force a refresh."
    )


def build_server(args) -> FastMCP:
    notes_dir = args.notes_dir.expanduser().resolve()
    if not notes_dir.is_dir():
        raise SystemExit(f"--notes-dir is not a directory: {notes_dir}")
    db_path = args.cache_dir.expanduser() if args.cache_dir else default_db_path(args.name, notes_dir)
    if db_path.suffix != ".db":  # treat as a directory
        db_path = db_path / f"{default_db_path(args.name, notes_dir).stem}.db"
    index = NotesIndex(notes_dir, db_path, args.sync_interval, extensions=args.extensions)
    index.maybe_sync()  # initial index runs in the background so the server starts immediately

    def with_note(text: str) -> str:
        if index.initial_sync_pending:
            return text + "\n\n(Note: the initial indexing is still running, so results may be incomplete. Retry shortly.)"
        return text

    mcp = FastMCP(args.name, instructions=build_instructions(args.name, args.description))

    @mcp.tool()
    def search_notes(query: str, limit: int = 10) -> str:
        """Full-text search over note sections, best matches first. Returns 'path — Note > Section'
        and a snippet per hit. Use read_note with that path (and section) to read more."""
        rows = index.search(query, max(1, min(limit, 100)))
        if not rows:
            return with_note("No results. Try different or fewer keywords, synonyms, or a prefix like term*.")
        return with_note("\n\n".join(f"{p} — {t}\n{s}" for p, t, s in rows))

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
        return (f"Synced: {s['added']} added, {s['updated']} updated, "
                f"{s['removed']} removed ({s['total']} total).")

    return mcp


def main():
    build_server(parse_args()).run()


if __name__ == "__main__":
    main()
