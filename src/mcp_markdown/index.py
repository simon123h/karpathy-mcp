"""SQLite FTS5 index of a notes directory: incremental background sync, search, links."""

import logging
import os
import posixpath
import re
import sqlite3
import threading
import time
from pathlib import Path

from .extract import ExtractError
from .parsing import DEFAULT_EXTENSIONS, parse_note, read_note_file, strip_ext

log = logging.getLogger("mcp-markdown")

DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024

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


class NotesIndex:
    def __init__(
        self,
        notes_dir: Path,
        db_path: Path,
        sync_interval: float = 60.0,
        startup_wait: float = 10.0,
        extensions: tuple[str, ...] = DEFAULT_EXTENSIONS,
        max_file_bytes: int | None = DEFAULT_MAX_FILE_BYTES,
    ):
        self.root = notes_dir.resolve()
        self.extensions = frozenset(extensions)
        self.max_file_bytes = max_file_bytes or None  # 0 / None: no limit
        self._warned_oversized: set[str] = set()
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

    def _inside_root(self, path: str) -> bool:
        try:
            return Path(path).resolve().is_relative_to(self.root)
        except (OSError, RuntimeError):  # unresolvable, e.g. a symlink loop
            return False

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
                        elif (
                            entry.is_file()
                            and not entry.name.startswith("~$")
                            and os.path.splitext(entry.name)[1][1:].lower() in self.extensions
                        ):
                            if entry.is_symlink() and not self._inside_root(entry.path):
                                log.warning(
                                    "Skipping symlink outside the notes directory: %s", entry.path
                                )
                                continue
                            rel = Path(entry.path).relative_to(self.root).as_posix()
                            st = entry.stat()
                            if self.max_file_bytes and st.st_size > self.max_file_bytes:
                                if rel not in self._warned_oversized:
                                    self._warned_oversized.add(rel)
                                    log.warning(
                                        "Skipping %s: %d bytes exceeds the %d byte limit",
                                        rel,
                                        st.st_size,
                                        self.max_file_bytes,
                                    )
                                continue
                            found[rel] = st.st_mtime
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
        """Walk and read files without the DB lock, so searches never wait on I/O."""
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
                    text = read_note_file(self.root / rel)
                except (
                    ExtractError
                ) as e:  # permanently unreadable: index as empty so it isn't retried every sync
                    log.warning("Cannot extract %s (indexed as empty): %s", rel, e)
                    text = ""
                except OSError as e:
                    log.warning("Cannot read %s: %s", rel, e)
                    continue
                loaded.append((rel, mtime, *parse_note(text, rel)))
            removed_paths = [r for r in known if r not in on_disk]
            with self.lock:
                for rel, mtime, title, sections, links in loaded:
                    self._drop(rel)
                    for n, (_level, heading, body) in enumerate(sections):
                        # first chunk carries the bare note title (used for listings)
                        chunk_title = (
                            title
                            if n == 0 or not heading or heading == title
                            else f"{title} > {heading}"
                        )
                        self.db.execute(
                            "INSERT INTO notes_fts (rel_path, title, content) VALUES (?, ?, ?)",
                            (rel, chunk_title, body),
                        )
                    if not sections:  # empty file: keep it listed
                        self.db.execute(
                            "INSERT INTO notes_fts (rel_path, title, content) VALUES (?, ?, '')",
                            (rel, title),
                        )
                    self.db.executemany(
                        "INSERT INTO links (src, target) VALUES (?, ?)",
                        [(rel, t) for t in sorted(links)],
                    )
                    self.db.execute(
                        "INSERT OR REPLACE INTO file_meta (rel_path, mtime) VALUES (?, ?)",
                        (rel, mtime),
                    )
                for rel in removed_paths:
                    self._drop(rel)
                    self.db.execute("DELETE FROM file_meta WHERE rel_path = ?", (rel,))
                self.db.commit()
            stats = {
                "added": sum(1 for x in loaded if x[0] not in known),
                "updated": sum(1 for x in loaded if x[0] in known),
                "removed": len(removed_paths),
                "total": len(on_disk),
            }
            log.info("sync: %s in %.2fs", stats, time.monotonic() - t0)
            return stats
        finally:
            self.last_sync = (
                time.monotonic()
            )  # also after a failure, so errors don't retrigger every search
            self.ready.set()

    def _background_sync(self) -> None:
        try:
            self._sync_locked()
        except Exception:
            log.exception("background sync failed")
        finally:
            self.sync_lock.release()

    def maybe_sync(self) -> None:
        """If the index is older than sync_interval, start a background rescan (never blocks)."""
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
        """Refresh a stale index in the background; before the first sync, wait for it (bounded)."""
        self.maybe_sync()
        if not self.ready.is_set():
            self.ready.wait(self.startup_wait)

    def _match(self, fts_query: str, limit: int) -> list[tuple[str, str, str]]:
        with self.lock:
            return self.db.execute(
                "SELECT rel_path, title, snippet(notes_fts, 2, '**', '**', '…', 24) "
                "FROM notes_fts WHERE notes_fts MATCH ? "
                "ORDER BY bm25(notes_fts, 0, 5.0, 1.0) LIMIT ?",
                (fts_query, limit),
            ).fetchall()

    def search(self, query: str, limit: int) -> list[tuple[str, str, str]]:
        """Search sections. Raw FTS5 syntax first; on a syntax error retry with prefix terms."""
        self._prepare()
        try:
            return self._match(query, limit)
        except sqlite3.OperationalError:
            terms = re.findall(r"\w+", query)
            if not terms:
                return []
            return self._match(" ".join(f'"{t}"*' for t in terms), limit)

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
                by_path.setdefault(strip_ext(p.lower()), p)
                by_stem.setdefault(posixpath.basename(strip_ext(p.lower())), p)

            def resolve(target: str):
                return by_path.get(target) or by_stem.get(target)

            outgoing, unresolved = [], []
            for (t,) in self.db.execute(
                "SELECT target FROM links WHERE src = ? ORDER BY target", (rel_path,)
            ):
                dest = resolve(t)
                if dest is None:
                    unresolved.append(t)
                elif dest != rel_path and dest not in outgoing:
                    outgoing.append(dest)
            me = strip_ext(rel_path.lower())
            keys = (me, posixpath.basename(me))
            backlinks = sorted(
                {
                    src
                    for src, t in self.db.execute(
                        "SELECT src, target FROM links WHERE target IN (?, ?)", keys
                    )
                    if src != rel_path and resolve(t) == rel_path
                }
            )
            return outgoing, unresolved, backlinks
