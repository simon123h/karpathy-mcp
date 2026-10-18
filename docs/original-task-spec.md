> **Historical document.** This is the original task specification the project was started from (written by
> Gemini, edited by the owner). It is kept unchanged below. For what is implemented today and where it differs,
> see [requirements.md](requirements.md#9-relationship-to-the-original-task-specification).

---

# **Task: Build a Configurable Markdown Knowledge Base MCP Server**

## **1\. Objective**

Create a standalone Python MCP (Model Context Protocol) server using the official mcp SDK (FastMCP) that indexes and searches local directories of Markdown files using an embedded SQLite FTS5 full-text search engine.

The server must accept the target directory as a CLI argument so multiple server instances can be launched pointing to different note repositories or wiki mirrors.

## **2\. Requirements & Constraints**

> 1. **CLI Parameterization:**

- \--notes-dir \<PATH\> (Required): Root directory of Markdown notes.
- \--name \<STRING\> (Optional, default "markdown-notes"): Display name for the MCP server instance.
- \--cache-dir \<PATH\> (Optional): Location for the SQLite index. Defaults to \~/.cache/mcp-markdown/\<name\>\_\<hash\>.db.
  > 2. **Deterministic & Lightweight Engine:**
- Use standard library sqlite3 with **FTS5** full-text search.
- Use PRAGMA journal_mode \= WAL; and PRAGMA synchronous \= NORMAL;.
- Never use print() statements (which corrupt stdio JSON-RPC transport). Use sys.stderr or Python's logging module configured to stderr only.
  > 3. **Incremental Synchronization (sync):**
- Must avoid re-reading unchanged files on startup.
- Walk the directory via os.walk / os.scandir reading only file metadata (st_mtime).
- Only read file bodies for new or modified files (mtime changed).
- Remove deleted files from the SQLite tables.
  > 4. **Resilience to Windows / Host Environment:**
- Safely handle UTF-8 decode errors with errors="replace".
- Protect against path traversal (reject any relative paths resolving outside \--notes-dir).

## **3\. SQLite Database Schema**

Create two tables inside the SQLite database:

`-- File tracking table`  
`CREATE TABLE IF NOT EXISTS file_meta (`  
 `rel_path TEXT PRIMARY KEY,`  
 `mtime REAL`  
`);`

`-- FTS5 full-text index`  
`CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(`  
 `rel_path UNINDEXED,`  
 `title,`  
 `content,`  
 `tokenize = 'unicode61 remove_diacritics 1'`  
`);`

## **4\. MCP Tools to Expose**

> 1. search_notes(query: str, limit: int \= 5\) \-\> list\[dict\]

- Sanitizes query (removes bare punctuation that triggers FTS5 syntax errors).
- Queries notes_fts using MATCH and orders by bm25(notes_fts).
- Uses SQLite's snippet(notes_fts, 2, '\>\>\>', '\<\<\<', '...', 24\) to provide match context.
- Returns: \[{"path": str, "title": str, "snippet": str, "score": float}\].
  > 2. get_note_outline(rel_path: str) \-\> dict
- Verifies path safety.
- Reads the file and extracts all Markdown headings (\#, \#\#, \#\#\#).
- Returns: {"path": rel_path, "headings": list\[str\]}.
  > 3. read_note(rel_path: str, section: str \= "") \-\> str
- If section is empty, returns the full file content.
- If section is provided, slices and returns only the text under that heading until the next heading of equal or higher level.
  > 4. refresh_index() \-\> dict
- Re-runs the incremental sync() and returns statistics (added, updated, deleted, skipped).

## **5\. Implementation Structure**

Create the project with the following file layout:

`mcp-markdown-server/`  
`├── pyproject.toml`  
`├── README.md`  
`└── server.py`

### **pyproject.toml**

`[project]`  
`name = "mcp-markdown-server"`  
`version = "0.1.0"`  
`description = "FastMCP server for local Markdown vaults using SQLite FTS5"`  
`requires-python = ">=3.10"`  
`dependencies = [`  
 `"mcp>=1.0.0",`  
`]`

`[project.scripts]`  
`mcp-markdown = "server:main"`

### **server.py implementation guidance**

`import argparse`  
`import hashlib`  
`import logging`  
`import os`  
`import re`  
`import sqlite3`  
`import sys`  
`from pathlib import Path`  
`from typing import Any`  
`from mcp.server.fastmcp import FastMCP`

`logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s: %(message)s")`  
`logger = logging.getLogger("mcp_markdown")`

`class MarkdownFTSIndex:`  
 `def __init__(self, root_dir: Path, db_path: Path):`  
 `self.root_dir = root_dir.resolve()`  
 `self.db_path = db_path.resolve()`  
 `self.db_path.parent.mkdir(parents=True, exist_ok=True)`  
 `self._init_db()`

    `def _get_connection(self) -> sqlite3.Connection:`
        `conn = sqlite3.connect(self.db_path)`
        `conn.execute("PRAGMA journal_mode = WAL;")`
        `conn.execute("PRAGMA synchronous = NORMAL;")`
        `return conn`

    `def _init_db(self) -> None:`
        `with self._get_connection() as conn:`
            `conn.execute("""`
                `CREATE TABLE IF NOT EXISTS file_meta (`
                    `rel_path TEXT PRIMARY KEY,`
                    `mtime REAL`
                `);`
            `""")`
            `conn.execute("""`
                `CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(`
                    `rel_path UNINDEXED,`
                    `title,`
                    `content,`
                    `tokenize = 'unicode61 remove_diacritics 1'`
                `);`
            `""")`

    `def sync(self) -> dict[str, int]:`
        `stats = {"added": 0, "updated": 0, "deleted": 0, "skipped": 0}`
        `with self._get_connection() as conn:`
            `cursor = conn.cursor()`
            `existing_meta = dict(cursor.execute("SELECT rel_path, mtime FROM file_meta").fetchall())`
            `found_paths = set()`

            `for root, _, files in os.walk(self.root_dir):`
                `for file in files:`
                    `if not file.endswith(".md"):`
                        `continue`

                    `full_path = Path(root) / file`
                    `try:`
                        `stat = full_path.stat()`
                    `except OSError:`
                        `continue`

                    `rel_path = full_path.relative_to(self.root_dir).as_posix()`
                    `found_paths.add(rel_path)`
                    `current_mtime = stat.st_mtime`

                    `if rel_path in existing_meta and existing_meta[rel_path] == current_mtime:`
                        `stats["skipped"] += 1`
                        `continue`

                    `try:`
                        `content = full_path.read_text(encoding="utf-8", errors="replace")`
                    `except OSError:`
                        `continue`

                    `title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)`
                    `title = title_match.group(1).strip() if title_match else full_path.stem`

                    `if rel_path in existing_meta:`
                        `cursor.execute("DELETE FROM notes_fts WHERE rel_path = ?", (rel_path,))`
                        `cursor.execute("INSERT INTO notes_fts(rel_path, title, content) VALUES (?, ?, ?)",`
                                       `(rel_path, title, content))`
                        `cursor.execute("UPDATE file_meta SET mtime = ? WHERE rel_path = ?",`
                                       `(current_mtime, rel_path))`
                        `stats["updated"] += 1`
                    `else:`
                        `cursor.execute("INSERT INTO notes_fts(rel_path, title, content) VALUES (?, ?, ?)",`
                                       `(rel_path, title, content))`
                        `cursor.execute("INSERT INTO file_meta(rel_path, mtime) VALUES (?, ?)",`
                                       `(rel_path, current_mtime))`
                        `stats["added"] += 1`

            `deleted_paths = set(existing_meta.keys()) - found_paths`
            `for dead_path in deleted_paths:`
                `cursor.execute("DELETE FROM file_meta WHERE rel_path = ?", (dead_path,))`
                `cursor.execute("DELETE FROM notes_fts WHERE rel_path = ?", (dead_path,))`
                `stats["deleted"] += 1`

            `conn.commit()`
        `return stats`

    `def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:`
        `clean_query = re.sub(r'[^\w\s]', ' ', query).strip()`
        `if not clean_query:`
            `return []`

        `terms = [f'"{term}"*' for term in clean_query.split()]`
        `fts_expr = " AND ".join(terms)`

        `with self._get_connection() as conn:`
            `cursor = conn.execute("""`
                `SELECT`
                    `rel_path,`
                    `title,`
                    `snippet(notes_fts, 2, '>>>', '<<<', '...', 24) AS excerpt,`
                    `bm25(notes_fts, 5.0, 1.0) AS rank`
                `FROM notes_fts`
                `WHERE notes_fts MATCH ?`
                `ORDER BY rank`
                `LIMIT ?`
            `""", (fts_expr, limit))`

            `return [`
                `{`
                    `"path": row[0],`
                    `"title": row[1],`
                    `"snippet": row[2].replace("\n", " "),`
                    `"score": round(row[3], 3)`
                `}`
                `for row in cursor.fetchall()`
            `]`

    `def _resolve_safe_path(self, rel_path: str) -> Path:`
        `target = (self.root_dir / rel_path).resolve()`
        `if not target.is_relative_to(self.root_dir) or not target.exists():`
            `raise FileNotFoundError("File not found or invalid path.")`
        `return target`

    `def get_outline(self, rel_path: str) -> dict[str, Any]:`
        `target = self._resolve_safe_path(rel_path)`
        `content = target.read_text(encoding="utf-8", errors="replace")`
        `headings = [line for line in content.splitlines() if re.match(r"^#{1,3}\s", line)]`
        `return {"path": rel_path, "headings": headings}`

    `def read_note(self, rel_path: str, section: str = "") -> str:`
        `target = self._resolve_safe_path(rel_path)`
        `content = target.read_text(encoding="utf-8", errors="replace")`
        `if not section:`
            `return content`

        `pattern = rf"(^#+\s+{re.escape(section)}.*?)(?=\n#+\s|\Z)"`
        `match = re.search(pattern, content, re.MULTILINE | re.DOTALL)`
        `if match:`
            `return match.group(1).strip()`
        `return f"Section '{section}' not found in {rel_path}."`

`def parse_args():`  
 `parser = argparse.ArgumentParser(description="Markdown Notes MCP Server")`  
 `parser.add_argument("--notes-dir", required=True, type=Path, help="Root folder of Markdown notes")`  
 `parser.add_argument("--name", default="markdown-notes", help="MCP server instance name")`  
 `parser.add_argument("--cache-dir", type=Path, default=None, help="Directory to store SQLite FTS database")`  
 `return parser.parse_args()`

`def main():`  
 `args = parse_args()`  
 `notes_dir = args.notes_dir.resolve()`  
 `if not notes_dir.is_dir():`  
 `logger.error(f"Provided path is not a directory: {notes_dir}")`  
 `sys.exit(1)`

    `if args.cache_dir:`
        `cache_dir = args.cache_dir.resolve()`
    `else:`
        `path_hash = hashlib.sha256(str(notes_dir).encode()).hexdigest()[:10]`
        `cache_dir = Path.home() / ".cache" / "mcp-markdown"`
    `cache_dir.mkdir(parents=True, exist_ok=True)`
    `db_path = cache_dir / f"{args.name}_{path_hash}.db"`

    `index = MarkdownFTSIndex(root_dir=notes_dir, db_path=db_path)`
    `index.sync()`

    `mcp = FastMCP(args.name)`

    `@mcp.tool()`
    `def search_notes(query: str, limit: int = 5) -> list[dict[str, Any]]:`
        `"""Search markdown notes by keyword using BM25 ranking."""`
        `return index.search(query, limit=limit)`

    `@mcp.tool()`
    `def get_note_outline(rel_path: str) -> dict[str, Any]:`
        `"""Inspect headings (H1-H3) in a note without reading the whole document."""`
        `try:`
            `return index.get_outline(rel_path)`
        `except FileNotFoundError as err:`
            `return {"error": str(err)}`

    `@mcp.tool()`
    `def read_note(rel_path: str, section: str = "") -> str:`
        `"""Read a full note or a specific heading section."""`
        `try:`
            `return index.read_note(rel_path, section=section)`
        `except FileNotFoundError as err:`
            `return f"Error: {err}"`

    `@mcp.tool()`
    `def refresh_index() -> dict[str, int]:`
        `"""Trigger an incremental index sync."""`
        `return index.sync()`

    `mcp.run()`

`if __name__ == "__main__":`  
 `main()`

## **6\. Verification & Test Plan**

> 1. **CLI Execution & stdio Check:** Verify that the server starts up cleanly without emitting debug text to stdout:  
>    `python server.py --notes-dir /path/to/test-notes --name test-wiki`

> 2. **Security & Path Traversal:** Call read_note("../../../some_secret.txt") and verify it returns an error rather than escaping the directory.
> 3. **Incremental Sync Verification:** Modify a single .md file, call refresh_index(), and confirm the returned dictionary reports updated \== 1 and marks unchanged files as skipped.
