# karpathy-mcp

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

## Quick start

Requires Python 3.10+. Install from a clone (not published on PyPI):

```bash
git clone <repo-url> karpathy-mcp && cd karpathy-mcp
pip install .            # or: pipx install .   /   uv tool install .
```

Register it with an MCP client, for example Claude Code:

```bash
claude mcp add my-notes -- mcp-markdown --notes-dir /home/me/notes --name my-notes
```

or in the configuration of any MCP client (such as Claude Desktop):

```json
{
  "mcpServers": {
    "my-notes": {
      "command": "mcp-markdown",
      "args": ["--notes-dir", "/home/me/notes", "--name", "my-notes"]
    }
  }
}
```

Run one instance per notes directory; `--description` tells the agent what each contains. More options and
details are in the [usage guide](docs/usage.md).

## Tools

| Tool | What it does |
|------|--------------|
| `search_notes(query, limit=5)` | Ranked search over note sections; returns `path — Note > Section` and a snippet. |
| `read_note(path, section="")` | Reads a whole note or just one heading section. |
| `get_outline(path)` | Heading structure with section sizes. |
| `list_notes(prefix="")` | Lists indexed notes, optionally by path prefix. |
| `get_links(path)` | Outgoing links and backlinks of a note. |
| `sync_notes()` | Rescans the notes directory immediately. |

The server also sends the agent short usage instructions, and `--description` lets you say what a given wiki
contains.

## Main options

| Option | Default | |
|--------|---------|---|
| `--notes-dir PATH` | required | Root directory of the notes |
| `--name TEXT` | `markdown-notes` | Server name; also part of the cache file name |
| `--description TEXT` | | Tells the agent what the notes are about |
| `--cache-dir PATH` | `~/.cache/mcp-markdown/` | Where the SQLite index lives |
| `--sync-interval SECONDS` | `60` | Maximum age of the index before a background rescan |
| `--extensions LIST` | all supported | e.g. `md,txt` to restrict formats |

Full reference: [docs/usage.md](docs/usage.md).

## Documentation

| Document | Contents |
|----------|----------|
| [docs/usage.md](docs/usage.md) | Installation, options, client configuration, formats, tool reference, behaviour notes |
| [docs/requirements.md](docs/requirements.md) | Vision, goals, functional and quality requirements, constraints ([req42](https://req42.de/)) |
| [docs/architecture.md](docs/architecture.md) | Building blocks, runtime view, decisions, risks ([arc42](https://arc42.org/)) |
| [docs/development.md](docs/development.md) | Setup, tests, conventions, recipes for new formats, tools and schema changes |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute |
| [docs/original-task-spec.md](docs/original-task-spec.md) | The original task specification this project started from |
| [docs/review-summary.md](docs/review-summary.md) | Summary of what was built and why, including deviations from the original specification |
| [LICENSE.md](LICENSE.md) | Apache License 2.0 |

## Project layout

```
src/mcp_markdown/   server.py (CLI + tools), index.py (SQLite index), parsing.py (formats), extract.py (.docx)
tests/              pytest suites
docs/               documentation
```

## Status

Early (version 0.1.0). Covered by unit tests and in-process tool calls; it has not yet been exercised with a real
MCP client in production use or on Windows. Known risks and open decisions are listed in
[architecture.md, section 11](docs/architecture.md#11-risks-and-technical-debt) and
[requirements.md, section 9](docs/requirements.md#9-relationship-to-the-original-task-specification).

## License

[Apache License 2.0](LICENSE.md)
