# karpathy-mcp

[![CI](https://github.com/simon123h/karpathy-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/simon123h/karpathy-mcp/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io/) server that gives an AI agent fast, read-only access to a local
knowledge base of notes: a Karpathy-style Markdown wiki, a mirror of a wiki, or any folder of text documents.
Notes are indexed into a single SQLite FTS5 file, so search and reading do not have to walk and open files on
every call, which matters on machines with slow file access (antivirus scanning, network drives).

## Features

- **Ranked full-text search** (SQLite FTS5, BM25, diacritic-insensitive) with snippets, at **section** level.
- **Navigation tools:** heading outlines with sizes, reading a single section, listing notes, links and backlinks
  (`[[wikilinks]]` and relative links).
- **Never blocks on the disk:** queries are answered from the index; a background rescan keeps it fresh
  (about a minute behind at most by default). Startup is instant.
- **Many formats:** Markdown, plain text, reStructuredText, Org, AsciiDoc and Word (`.docx`) with no extra dependencies.
- **Light and safe:** standard library plus the `mcp` SDK, one cache file per instance, read-only, path traversal
  protection.
- **One instance per notes directory:** run as many as you need, each with its own name and description.

## Installation

Requires Python 3.10 or newer (Linux, macOS, Windows). The package is not published on PyPI; install it from a
clone of this repository. The distribution is called `karpathy-mcp`, the command it installs `mcp-markdown`.

The recommended way is an isolated tool install, which puts `mcp-markdown` on your `PATH` without touching other
Python environments:

```bash
git clone https://github.com/simon123h/karpathy-mcp.git karpathy-mcp && cd karpathy-mcp
uv tool install .          # or: pipx install .
mcp-markdown --help        # check that it works
```

Other ways to install or run it:

| Goal                                    | Command                                                                                                                                 |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Install straight from GitHub (no clone) | `uv tool install git+https://github.com/simon123h/karpathy-mcp.git` or `pipx install git+https://github.com/simon123h/karpathy-mcp.git` |
| Run without installing (needs uv)       | `uvx --from /path/to/karpathy-mcp mcp-markdown --notes-dir ...`                                                                         |
| Install into the current virtualenv     | `pip install .`                                                                                                                         |
| Develop on the code                     | `pip install -e '.[dev]'` (see [development guide](docs/development.md))                                                                |
| Upgrade after `git pull`                | `uv tool install --reinstall .`, `pipx install --force .` or `pip install -U .`                                                         |
| Uninstall                               | `uv tool uninstall karpathy-mcp`, `pipx uninstall karpathy-mcp` or `pip uninstall karpathy-mcp`                                         |

The index is a cache and lives in `~/.cache/mcp-markdown/` (on Windows `C:\Users\<you>\.cache\mcp-markdown\`).
Use `--cache-dir` to put it elsewhere. It is created on first start, in the background, and can be deleted at any time.

## Connect to a client

Run one instance per notes directory; `--name` and `--description` tell the agent what each one contains.

**Claude Code:**

```bash
claude mcp add my-notes -- mcp-markdown --notes-dir /home/me/notes --name my-notes
```

**Claude Desktop and other clients** use a JSON configuration (Claude Desktop:
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json`
on Windows):

```json
{
  "mcpServers": {
    "my-notes": {
      "command": "mcp-markdown",
      "args": [
        "--notes-dir",
        "/home/me/notes",
        "--name",
        "my-notes",
        "--description",
        "Personal engineering notes"
      ]
    }
  }
}
```

Use absolute paths for `--notes-dir`. On Windows write them with forward slashes (`C:/Users/me/notes`) or escape
backslashes in JSON (`C:\\Users\\me\\notes`).

**If the client cannot start the server,** it probably does not see your shell's `PATH` (desktop apps often start with
a minimal one). Give the absolute path of the executable as `command`: find it with `which mcp-markdown` (macOS,
Linux) or `where mcp-markdown` (Windows). Alternatively let uv launch it, which needs no install step (the first launch is slower while uv builds the
package):

```json
{
  "mcpServers": {
    "my-notes": {
      "command": "uvx",
      "args": [
        "--from",
        "/path/to/karpathy-mcp",
        "mcp-markdown",
        "--notes-dir",
        "/home/me/notes"
      ]
    }
  }
}
```

Server logs go to stderr, which most clients write to their MCP log files. Options and details are in the
[usage guide](docs/usage.md).

## Tools

| Tool                           | What it does                                                                     |
| ------------------------------ | -------------------------------------------------------------------------------- |
| `search_notes(query, limit=5)` | Ranked search over note sections; returns `path — Note > Section` and a snippet. |
| `read_note(path, section="")`  | Reads a whole note or just one heading section.                                  |
| `get_outline(path)`            | Heading structure with section sizes.                                            |
| `list_notes(prefix="")`        | Lists indexed notes, optionally by path prefix.                                  |
| `get_links(path)`              | Outgoing links and backlinks of a note.                                          |
| `sync_notes()`                 | Rescans the notes directory immediately.                                         |

The server also sends the agent short usage instructions, and `--description` lets you say what a given wiki
contains.

## Main options

| Option                    | Default                  |                                                     |
| ------------------------- | ------------------------ | --------------------------------------------------- |
| `--notes-dir PATH`        | required                 | Root directory of the notes                         |
| `--name TEXT`             | `markdown-notes`         | Server name; also part of the cache file name       |
| `--description TEXT`      |                          | Tells the agent what the notes are about            |
| `--cache-dir PATH`        | `~/.cache/mcp-markdown/` | Where the SQLite index lives                        |
| `--sync-interval SECONDS` | `60`                     | Maximum age of the index before a background rescan |
| `--extensions LIST`       | all supported            | e.g. `md,txt` to restrict formats                   |
| `--max-file-size MB`      | `10`                     | Skip larger files (`0` = no limit)                  |

Full reference: [docs/usage.md](docs/usage.md).

## Documentation

| Document                                                 | Contents                                                                                     |
| -------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| [docs/usage.md](docs/usage.md)                           | Installation, options, client configuration, formats, tool reference, behaviour notes        |
| [docs/requirements.md](docs/requirements.md)             | Vision, goals, functional and quality requirements, constraints ([req42](https://req42.de/)) |
| [docs/architecture.md](docs/architecture.md)             | Building blocks, runtime view, decisions, risks ([arc42](https://arc42.org/))                |
| [docs/development.md](docs/development.md)               | Setup, tests, conventions, recipes for new formats, tools and schema changes                 |
| [CONTRIBUTING.md](CONTRIBUTING.md)                       | How to contribute                                                                            |
| [docs/original-task-spec.md](docs/original-task-spec.md) | The original task specification this project started from                                    |
| [LICENSE.md](LICENSE.md)                                 | Apache License 2.0                                                                           |

## Project layout

```
src/mcp_markdown/   server.py (CLI + tools), index.py (SQLite index), parsing.py (formats), extract.py (.docx)
tests/              pytest suites
docs/               documentation
```

## Status

Early (version 0.1.0). Covered by unit tests and end-to-end stdio tests (the real server driven by an MCP client
over stdio). CI runs them on Linux, Windows and macOS with Python 3.10-3.13 (see the badge above). The server has
not yet been used in daily work with a real client or on the Windows machine it was designed for. Known risks and open decisions are listed in
[architecture.md, section 11](docs/architecture.md#11-risks-and-technical-debt) and
[requirements.md, section 9](docs/requirements.md#9-relationship-to-the-original-task-specification).

## License

[Apache License 2.0](LICENSE.md)
