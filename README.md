# Task: Build a Configurable Markdown Knowledge Base MCP Server

## 1. Objective

Create a standalone Python MCP (Model Context Protocol) server using the official `mcp` SDK (`FastMCP`) that indexes and searches local directories of Markdown files using an embedded SQLite FTS5 full-text search engine.

The server must accept the target directory as a CLI argument so multiple server instances can be launched pointing to different note repositories or wiki mirrors.

---

## 2. Requirements & Constraints

1. **CLI Parameterization:**
   - `--notes-dir <PATH>` (Required): Root directory of Markdown notes.
   - `--name <STRING>` (Optional, default `"markdown-notes"`): Display name for the MCP server instance.
   - `--cache-dir <PATH>` (Optional): Location for the SQLite index. Defaults to `~/.cache/mcp-markdown/<sanitized_name_or_hash>.db`.
2. **Deterministic & Lightweight Engine:**
   - Use standard library `sqlite3` with **FTS5** full-text search.
   - Use `PRAGMA journal_mode = WAL;` and `PRAGMA synchronous = NORMAL;`.
   - Never use `print()` statements (which corrupt `stdio` JSON-RPC transport). Use `sys.stderr` or Python's `logging` module configured to `stderr` only.
3. **Incremental Synchronization (`sync`):**
   - Must avoid re-reading unchanged files on startup.
   - Walk the directory via `os.walk` / `os.scandir` reading only file metadata (`st_mtime`).
   - Only read file bodies for new or modified files (`mtime` changed).
   - Remove deleted files from the SQLite tables.
4. **Resilience to Windows / Host Environment:**
   - Safely handle UTF-8 decode errors with `errors="replace"`.
   - Protect against path traversal (reject any relative paths resolving outside `--notes-dir`).

---

## 3. SQLite Database Schema

Create two tables inside the SQLite database:

```sql
-- File tracking table
CREATE TABLE IF NOT EXISTS file_meta (
    rel_path TEXT PRIMARY KEY,
    mtime REAL
);

-- FTS5 full-text index
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    rel_path UNINDEXED,
    title,
    content,
    tokenize = 'unicode61 remove_diacritics 1'
);
```
