# Contributing

Thanks for your interest in improving karpathy-mcp. This is a small project; the aim is to keep it small,
predictable and easy to read.

## Getting started

```bash
git clone https://github.com/simon123h/karpathy-mcp.git karpathy-mcp && cd karpathy-mcp
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pre-commit install     # runs ruff on every commit
pytest -q
```

More detail (layout, running the server by hand, recipes) is in [docs/development.md](docs/development.md). To
understand why things are the way they are, read [docs/architecture.md](docs/architecture.md), especially the
decisions in section 9 and the risks in section 11.

## Before you start

- For anything beyond a small fix, open an issue (or discuss it with the maintainer) first, so effort is not
  wasted on something that conflicts with the goals in [docs/requirements.md](docs/requirements.md).
- Good first contributions: items from the risk list (architecture section 11) and the future requirements
  (requirements section 8), such as `--exclude` globs, the cache-name collision fix, or measuring sync on large trees.
- Out of scope by design: write tools enabled by default, network services, semantic search as a hard dependency.

## Making a change

1. Create a branch from `main`.
2. Make the change. Follow the surrounding code: naming, comment density, idioms. Keep functions short and
   comments about _why_, not _what_.
3. Add or update tests for behaviour you change or add. Bug fixes should come with a test that fails without the fix.
4. Run `pytest -q` and `ruff check . && ruff format --check .`; everything must pass. The pre-commit hook
   runs the same ruff checks (and fixes what it can) when you commit. CI repeats lint, format and tests on Linux,
   Windows and macOS (see [docs/development.md](docs/development.md#continuous-integration)).
5. Update the documentation that your change touches (see the checklist below).
6. Commit with a short, imperative subject line describing the change (for example
   `Add --exclude option for ignoring files`). Keep commits focused; do not mix refactoring and behaviour changes.
7. Open a pull request describing what changed and why, and how you verified it.

## Rules that must not be broken

- **No `print()` and nothing but protocol traffic on stdout.** Logging goes to stderr.
- **No file access on the query path.** Searches and reads are answered from the index; slow work happens in
  the background sync.
- **All database access under `NotesIndex.lock`**; schema changes bump `SCHEMA_VERSION`.
- **Tool arguments are untrusted:** resolve paths with `NotesIndex.resolve`; never read outside the notes directory.
- **Ruff must be clean.** Linting (`ruff check`) and formatting (`ruff format`, line length 100) are enforced
  by the pre-commit hook; configuration lives in `pyproject.toml`.
- **Keep runtime dependencies minimal.** The standard library plus `mcp`. Adding a dependency needs a good reason,
  and an optional one (an extra in `pyproject.toml`) is preferred over a required one.

## Documentation checklist

| If you change...                                              | Update                                                            |
| ------------------------------------------------------------- | ----------------------------------------------------------------- |
| CLI options, tools, formats, behaviour users see              | [docs/usage.md](docs/usage.md) and the README summary if affected |
| Goals, requirements, scope                                    | [docs/requirements.md](docs/requirements.md)                      |
| Structure, schema, concurrency, a design decision, a new risk | [docs/architecture.md](docs/architecture.md)                      |
| Setup, tests, tooling, recipes                                | [docs/development.md](docs/development.md)                        |

## Reporting problems

Please include: the command line you used (without private paths if you prefer), your OS and Python version,
the `mcp` version (`pip show mcp`), the relevant stderr output of the server, and, if possible, a minimal notes
directory that reproduces the problem. Security-relevant problems (for example path handling) should be reported
privately to the maintainer rather than in a public issue.

## License

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE.md), the
license of this project.
