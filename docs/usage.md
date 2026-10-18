# Usage

Reference for installing, configuring and running the server, and for the tools it exposes to an agent.

## Installation

The package is not published on PyPI; install it from a clone. Python 3.10 or newer is required.

```bash
git clone <repo-url> karpathy-mcp && cd karpathy-mcp
pip install .            # or: pipx install .   /   uv tool install .
```

This provides the `mcp-markdown` command and the module entry point `python -m mcp_markdown`. Without
installing, run it from the repository with `PYTHONPATH=src python -m mcp_markdown ...` (the `mcp` package must
be importable).

## Running

```bash
mcp-markdown --notes-dir ~/notes --name my-notes
```

The server speaks MCP over stdio. It starts immediately and builds the index in the background; logs go to stderr.

### Command-line options

| Option | Default | Description |
|--------|---------|-------------|
| `--notes-dir PATH` | required | Root directory of the notes. |
| `--name TEXT` | `markdown-notes` | Display name of the MCP server. Also determines the default index file name (see below). |
| `--description TEXT` | empty | What these notes contain. Shown to the agent in the server instructions; useful when running several instances. |
| `--cache-dir PATH` | `~/.cache/mcp-markdown/` | Where to keep the SQLite index. A path ending in `.db` is used as the database file; any other path is treated as a directory. |
| `--sync-interval SECONDS` | `60` | Minimum time between background rescans. A search or listing that finds the index older than this starts a rescan (it does not wait for it). `0` rescans on every query. |
| `--extensions LIST` | all supported | Comma-separated extensions to index, e.g. `md,txt`. Unsupported values are rejected. |

The default index file is `~/.cache/mcp-markdown/<name>_<hash>.db`: the name sanitized to `[A-Za-z0-9_.-]` plus
a short hash of the notes directory, so several instances can share a name, or one `--cache-dir`, without
sharing an index.

The index is a cache. It can be deleted at any time and is rebuilt on the next start.

### Client configuration

Generic MCP client configuration (for example Claude Desktop's `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "my-notes": {
      "command": "mcp-markdown",
      "args": ["--notes-dir", "/home/me/notes", "--name", "my-notes",
               "--description", "Personal engineering notes"]
    }
  }
}
```

Claude Code:

```bash
claude mcp add my-notes -- mcp-markdown --notes-dir /home/me/notes --name my-notes
```

Use absolute paths. If the command is not on the client's `PATH`, give the full path to the executable.

## Supported formats

| Extension | Sections | Links | Notes |
|-----------|----------|-------|-------|
| `md`, `markdown`, `mdx` | `#` headings (levels 1-6); headings inside fenced code are ignored | `[[wikilink]]`, `[[target\|alias]]`, `[[target#section]]`, relative `[text](path.md)` | YAML frontmatter stays in the first section. |
| `txt`, `text`, `rst` | one section | none | |
| `org` | `*` headings; trailing `:tags:` stripped; `#+BEGIN_`/`#+END_` blocks ignored | none | |
| `adoc`, `asciidoc` | `=` headings; `----` and `....` blocks ignored | none | |
| `docx` | Word heading styles (including localized and custom heading styles) | none | Converted to text first. Tables become `a \| b` rows, list items `- `. Images, headers/footers, footnotes and comments are not extracted. Word's `~$` lock files are skipped. |

A note's **title** is its first level-1 heading, otherwise the file name without extension. Hidden directories
(names starting with `.`) and symbolic links to directories are not scanned. Links can point to a note of any
format; if two notes share a name (`a.md`, `a.txt`), a bare `[[a]]` resolves to the first path alphabetically.

## Tools

All tools are read-only and return plain text. Paths are relative to `--notes-dir` and use `/` (backslashes are
accepted). Paths that resolve outside the notes directory are rejected.

### `search_notes(query, limit=10)`

Full-text search over note **sections**, best matches first (BM25; title matches weigh five times body matches).
`limit` is clamped to 1-100. Each hit is `path — Note title > Section heading` followed by a snippet with matches
marked as `**term**`.

Queries use [FTS5 syntax](https://www.sqlite.org/fts5.html#full_text_query_syntax): plain words are ANDed;
`"exact phrase"`, `prefix*`, `OR`, `NOT` are available. Text that is not valid FTS5 (for example `foo-bar` or
`error: timeout`) is retried automatically as quoted, ANDed words. Matching is keyword-based and diacritics are
ignored (`cafe` finds `café`).

### `read_note(path, section="")`

Returns the whole note, or, if `section` is given, only the section with that heading (case-insensitive) including
its subsections. `Note > Section` as printed by a search hit is accepted. An unknown section returns the note's
outline. Content comes from the index, so it can be up to one sync interval old; line endings are normalized
to `\n`.

### `get_outline(path)`

The heading structure with the size of each section, e.g. `## Deployment (1.2k chars)`. Use it to choose which
section to read from a long note.

### `list_notes(prefix="")`

Indexed notes as `path — title`, optionally restricted to a path prefix such as `projects/`.

### `get_links(path)`

Notes this note links to, unresolved links (no matching note), and notes that link to it (backlinks).

### `sync_notes()`

Rescans immediately and waits for the result: `Synced: N added, N updated, N removed (N total).`

## Behaviour to be aware of

- **Freshness:** edits become visible after the next background rescan, typically within one `--sync-interval`
  plus the next query. A query never waits for a rescan.
- **First start:** the first search waits up to 10 seconds for the initial index. If it is still running, results
  are returned with a note saying they may be incomplete.
- **Unreadable files:** files that cannot be read (for example locked by another program) are skipped and retried
  on the next rescan. Corrupt `.docx` files are logged once and indexed as empty.
- **Slow file systems:** the rescan reads only file metadata for unchanged files, but it still walks the whole
  tree. Raise `--sync-interval` (for example `600`) on slow disks and use `sync_notes` when a refresh is needed.
