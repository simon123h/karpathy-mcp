# Development guide

How to set up, test, extend and debug the project. Design background is in [architecture.md](architecture.md);
contribution rules are in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Setup

Python 3.10 or newer. The project is developed and tested on Linux with Python 3.12 and `mcp` 1.27.2.

```bash
python3 -m venv .venv && . .venv/bin/activate     # fish: source .venv/bin/activate.fish
pip install -e '.[dev]'
pre-commit install
```

The editable install provides the `mcp-markdown` command and puts `src/` on the import path. The `dev` extra
adds pytest, ruff and pre-commit; `pre-commit install` activates the commit hook described below.

## Repository layout

```
.
├── src/mcp_markdown/
│   ├── server.py      CLI, server instructions, MCP tool definitions
│   ├── index.py       NotesIndex: schema, sync, search, text retrieval, links
│   ├── parsing.py     formats, section splitting, link extraction, section lookup
│   ├── extract.py     .docx to Markdown text (stdlib only)
│   └── __main__.py    python -m mcp_markdown
├── tests/             pytest suites (test_index.py, test_extract.py)
├── docs/              usage, requirements (req42), architecture (arc42), this guide
├── pyproject.toml     packaging, entry point, pytest and ruff settings
├── .pre-commit-config.yaml   commit hook: ruff lint + format
├── CONTRIBUTING.md
└── LICENSE.md         Apache-2.0
```

## Running the tests

```bash
pytest -q
```

`pyproject.toml` sets `pythonpath = ["src"]`, so the tests also run without installing the package. They use
temporary directories only and finish in about a second.

Conventions for tests:

- Test the index through `NotesIndex` methods against files written into `tmp_path`.
- Use `idx.sync()` (blocking) to set up state. To test background behaviour, gate the scan with a
  `threading.Event` via `monkeypatch.setattr(idx, "_scan", ...)` and use `idx.wait_idle()`; see
  `test_search_does_not_wait_for_rescan`.
- Control modification times with `os.utime` (the `write(path, text, mtime=...)` helper) instead of sleeping.
- Build `.docx` fixtures in memory with `make_docx` in `tests/test_extract.py`.

## Code style: ruff and pre-commit

Linting and formatting are done by [ruff](https://docs.astral.sh/ruff/), configured in `pyproject.toml`
(line length 100, target Python 3.10; rule sets `E`, `W`, `F`, `I` (import order), `B`, `UP`, `SIM`).

```bash
ruff check .            # lint (add --fix for safe autofixes)
ruff format .           # format
ruff format --check .   # what the hook and a CI job would verify
```

The pre-commit hook (`.pre-commit-config.yaml`, installed with `pre-commit install`) runs `ruff check --fix` and
`ruff format` on the files of each commit. If either changes a file or finds a problem, the commit is
**rejected**; review the changes, `git add` them and commit again. To run the hooks on the whole tree:
`pre-commit run --all-files`.

The ruff version is pinned in two places that must be updated together: `ruff==...` in the `dev` extra of
`pyproject.toml` and `rev:` in `.pre-commit-config.yaml`. `pre-commit autoupdate` bumps the hook; then align the pin.

## Trying the server by hand

Run it against a scratch directory:

```bash
mkdir -p /tmp/notes && printf '# Hello\nworld\n## Setup\nsteps\n' > /tmp/notes/a.md
mcp-markdown --notes-dir /tmp/notes --cache-dir /tmp/notes-cache
```

The process waits for JSON-RPC on stdin; to exercise the tools without a client, call them in-process:

```python
import asyncio
from mcp_markdown.server import build_server, parse_args

mcp = build_server(parse_args(["--notes-dir", "/tmp/notes", "--cache-dir", "/tmp/notes-cache"]))

async def main():
    for tool, args in [("search_notes", {"query": "steps"}),
                       ("get_outline", {"path": "a.md"}),
                       ("read_note", {"path": "a.md", "section": "Setup"})]:
        print((await mcp.call_tool(tool, args))[1]["result"])

asyncio.run(main())
```

For a real client session, the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) can launch the
server over stdio (for example `npx @modelcontextprotocol/inspector mcp-markdown --notes-dir /tmp/notes`).

## Rules that keep the server working

- **Never write to stdout.** It carries the protocol. Use `logging` (configured to stderr in `main()`); no `print`.
- **Keep the query path free of file access.** Anything slow (directory walk, file reads, parsing) belongs in the
  sync, outside `NotesIndex.lock`.
- **All database access goes through `NotesIndex.lock`**; there is one shared connection.
- **Tools return strings and never raise** for expected problems (bad path, unknown section); return `Error: ...`.
- **Paths from tools are untrusted.** Pass them through `NotesIndex.resolve` before touching the file system.

## Recipes

### Add a file format

1. Add the extension to `SUPPORTED_EXTENSIONS` in `parsing.py`.
2. If it has headings, add a spec (heading regex, fence regex, whether links/frontmatter apply, heading clean-up)
   and register it in `_FORMATS`. Formats without a spec are indexed as a single section.
3. If it is binary, add a converter in `extract.py` that returns Markdown-like text and raises `ExtractError` for
   unparseable files, and call it from `read_note_file`. Make it independent of third-party packages if possible,
   or declare an optional dependency in `pyproject.toml`.
4. Add tests (sections, links, corrupt input) and update the format table in [usage.md](usage.md#supported-formats).

### Add or change a tool

1. Implement it inside `build_server` in `server.py`. Give it a docstring written for the agent: it is the tool
   description the model sees.
2. Mention it in `build_instructions` if it changes the recommended workflow.
3. Put logic that needs the database into `NotesIndex` so it can be tested without MCP.
4. Update [usage.md](usage.md#tools) and, if the contract changes, [requirements.md](requirements.md).

### Change the index layout

1. Edit `SCHEMA` in `index.py` and **increase `SCHEMA_VERSION`**. Older databases are dropped and rebuilt
   automatically.
2. Add a test that creates an old-layout database (see `test_old_schema_is_rebuilt`).
3. Update the schema table in [architecture.md](architecture.md#level-2-the-index).

## Versioning and releases

The version lives in `pyproject.toml` (`0.1.0`). There is no release automation or CI configuration yet; build a
wheel locally with `pip wheel --no-deps .` if needed.
