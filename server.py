#!/usr/bin/env python3
"""Configurable Markdown knowledge base MCP server backed by SQLite FTS5."""

import argparse
import hashlib
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("mcp-markdown")

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
"""

HEADING_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


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
                m = HEADING_RE.search(text)
                title = m.group(1) if m else Path(rel).stem
                self.db.execute("DELETE FROM notes_fts WHERE rel_path = ?", (rel,))
                self.db.execute(
                    "INSERT INTO notes_fts (rel_path, title, content) VALUES (?, ?, ?)",
                    (rel, title, text),
                )
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
                self.db.execute("DELETE FROM notes_fts WHERE rel_path = ?", (rel,))
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

    def search(self, query: str, limit: int) -> list[tuple[str, str, str]]:
        self.maybe_sync()
        with self.lock:
            return self.db.execute(
                "SELECT rel_path, title, snippet(notes_fts, 2, '**', '**', '…', 24) "
                "FROM notes_fts WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts, 0, 5.0, 1.0) LIMIT ?",
                (query, limit),
            ).fetchall()

    def list(self, prefix: str) -> list[tuple[str, str]]:
        self.maybe_sync()
        with self.lock:
            return self.db.execute(
                "SELECT rel_path, title FROM notes_fts WHERE rel_path LIKE ? ESCAPE '\\' ORDER BY rel_path",
                (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
            ).fetchall()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Markdown notes MCP server (SQLite FTS5)")
    p.add_argument("--notes-dir", required=True, type=Path, help="Root directory of Markdown notes")
    p.add_argument("--name", default="markdown-notes", help="MCP server display name")
    p.add_argument("--cache-dir", type=Path, default=None, help="Location for the SQLite index")
    p.add_argument("--sync-interval", type=float, default=60.0,
                   help="Max seconds between automatic rescans, checked on search/list (default: 60)")
    return p.parse_args(argv)


def default_db_path(name: str, notes_dir: Path) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
    if not safe:
        safe = hashlib.sha1(str(notes_dir).encode()).hexdigest()[:16]
    return Path.home() / ".cache" / "mcp-markdown" / f"{safe}.db"


def build_server(args) -> FastMCP:
    notes_dir = args.notes_dir.expanduser().resolve()
    if not notes_dir.is_dir():
        raise SystemExit(f"--notes-dir is not a directory: {notes_dir}")
    db_path = args.cache_dir.expanduser() if args.cache_dir else default_db_path(args.name, notes_dir)
    if db_path.suffix != ".db":  # treat as a directory
        db_path = db_path / f"{default_db_path(args.name, notes_dir).stem}.db"
    index = NotesIndex(notes_dir, db_path, args.sync_interval)
    index.sync()

    mcp = FastMCP(args.name)

    @mcp.tool()
    def search_notes(query: str, limit: int = 10) -> str:
        """Full-text search the notes (FTS5 syntax: terms, "phrases", prefix*, AND/OR/NOT)."""
        try:
            rows = index.search(query, max(1, min(limit, 100)))
        except sqlite3.OperationalError as e:
            return f"Invalid search query: {e}"
        if not rows:
            return "No results."
        return "\n\n".join(f"{p} — {t}\n{s}" for p, t, s in rows)

    @mcp.tool()
    def read_note(path: str) -> str:
        """Read a note by its path relative to the notes directory."""
        try:
            full = index.resolve(path)
            return full.read_text(encoding="utf-8", errors="replace")
        except ValueError as e:
            return f"Error: {e}"
        except OSError as e:
            return f"Error: cannot read {path}: {e.strerror}"

    @mcp.tool()
    def list_notes(prefix: str = "") -> str:
        """List indexed notes, optionally filtered by a relative path prefix."""
        rows = index.list(prefix)
        return "\n".join(f"{p} — {t}" for p, t in rows) or "No notes."

    @mcp.tool()
    def sync_notes() -> str:
        """Re-scan the notes directory and update the index with changes."""
        s = index.sync()
        return (f"Synced: {s['added']} added, {s['updated']} updated, "
                f"{s['removed']} removed ({s['total']} total).")

    return mcp


def main():
    build_server(parse_args()).run()


if __name__ == "__main__":
    main()
