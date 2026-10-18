# karpathy-mcp: what was built and why (handoff for review)

This summary was written by Claude (Claude Code session) for hand-over to Gemini, who wrote the original task
spec (the README, now preserved as `docs/original-task-spec.md`) and has more background on the project than the
building session did. It tries to be explicit about where the implementation **differs from that spec**, because
those differences are the main thing worth a second opinion.

> **Point-in-time document.** Sections 1-6 describe the state at hand-over to Gemini. Section 7 records the
> outcome of that review and what was changed afterwards.

## 1. Purpose, as understood

An MCP server that lets an AI agent search and read a local knowledge base of notes (Karpathy-style Markdown wiki,
or similar), backed by a single SQLite FTS5 file. One server instance per notes directory (`--notes-dir`).

Why a search index at all, rather than letting the agent grep and read files: the owner's stated reason is that on
one of their machines file access is slow (antivirus scanning, network drives). An index answers searches from one
file instead of walking and opening many. The ranking and snippet arguments (BM25, small answers instead of many
raw hits) are secondary but real.

## 2. What exists now

A Python package `mcp_markdown` (under `src/`), about 700 lines, 45 passing tests. Tools exposed (all read-only):

| Tool | Purpose |
|------|---------|
| `search_notes(query, limit=10)` | FTS5/BM25 search over **sections** of notes; returns `path — Note > Section` plus a snippet |
| `read_note(path, section="")` | Whole note, or just one heading section (with its subsections) |
| `get_outline(path)` | Heading tree with per-section sizes |
| `list_notes(prefix="")` | Indexed notes by path prefix |
| `get_links(path)` | Outgoing links and backlinks (`[[wikilinks]]` and relative links) |
| `sync_notes()` | Force a rescan |

The server also sends usage instructions to the agent (what the tools are for, a search -> outline -> section
workflow) and takes `--description` to say what a given wiki contains.

Formats (`--extensions`, default all): Markdown family, `.txt`/`.text`/`.rst` (single section), `.org`,
`.adoc`/`.asciidoc` (real sections), and `.docx` (stdlib-only extraction, Word headings become sections). No PDF.

## 3. Key design decisions and the reasoning

1. **SQLite FTS5 only, standard library, no embeddings.** Technical notes are mostly keyword-driven; zero extra
   dependencies beyond `mcp`; one cache file.
2. **Section-level rows.** Each heading section is one `notes_fts` row (title column = `Note > Heading`). Gives
   precise hits and lets `read_note(section=...)` and `get_outline` save context. The two spec tables are unchanged;
   an extra `links` table and `PRAGMA user_version` (rebuild on layout change) were added.
3. **Reads are served from the index**, not the disk, for the same slow-disk reason. Trade-off: reads can be up to
   one sync interval stale, and a deleted note stays readable until the next sync.
4. **Stale-while-revalidate sync.** Instead of a timer thread: a query that finds the index older than
   `--sync-interval` (default 60 s) answers immediately and starts one background rescan. The directory walk and
   file reads run outside the DB lock. The initial index is also built in the background, so startup is instant
   (a first search waits up to 10 s, then answers with a note that indexing is incomplete). The owner said missing
   an edit of the last minute is fine.
5. **Query robustness.** Raw FTS5 syntax is tried first; on a syntax error it retries with each word quoted
   (AND). Chosen so agents' natural-language queries (`foo-bar`, `a: b`) don't fail.
6. **Output is plain text, not JSON objects**, to keep tokens low and easy for models to read.
7. **Links resolve lazily** at query time (so a link to a note created later still works). Ambiguous names resolve
   to the first path alphabetically.
8. **Corrupt/unparseable files** (e.g. a broken `.docx`) are indexed as empty and logged once, so they are not
   re-parsed on every sync.

## 4. Deviations from the original spec (please review)

The building session worked from a **shorter version** of the README (objective, CLI flags, schema only). The
extended version (tool signatures, reference implementation, test plan) was first seen during the final
restructuring; it is unknown when it was added. So several differences are **unintentional drift**, not considered
rejections of the spec:

| Spec | Implementation | Comment |
|------|----------------|---------|
| `get_note_outline(rel_path)` -> `dict` with H1-H3 headings | `get_outline(path)` -> text, all levels, with sizes | name and shape differ |
| `refresh_index()` -> `dict` (`added/updated/deleted/skipped`) | `sync_notes()` -> text (`added/updated/removed/total`) | name and keys differ |
| `search_notes(query, limit=5)` -> `list[dict]` with `path,title,snippet,score` | `limit=10`, text, no score, `**` highlight markers | name kept; contract differs |
| Parameter name `rel_path` | `path` | |
| Query: strip punctuation, AND of prefix-matched terms (`"term"*`) | raw FTS5 first, quoted AND fallback (no prefix matching) | possibly worth adding prefix matching |
| Cache file `<name>_<hash-of-notes-dir>.db` | `<sanitized-name>.db` | **real risk:** two instances with the same `--name` and different dirs share one DB |
| Whole-file rows | one row per section | schema columns unchanged |
| Index built synchronously on start | built in the background | avoids client start-up timeouts on slow disks |
| `.md` only | extension list, default all supported | owner asked for broader formats |

## 5. Known risks and open points

- Cache-name collision described above (easy fix: add the notes-dir hash, as the spec says).
- Files reached via a **symlink pointing outside** the notes directory are still indexed, because the scan follows
  file symlinks. Tool path arguments are contained, but the sync is not. Low risk for personal notes, not nil.
- Default "all extensions" can add noise (`requirements.txt`, logs) and duplicates when `a.md` and `a.docx` coexist;
  a possible `--exclude` option has not been built.
- Each sync walks the whole tree (metadata only). On the slow machine this is the remaining cost; it has **not been
  measured** (sync duration is now logged). An adaptive interval is an option.
- **Not tested against a real MCP client over stdio.** Verification so far: unit tests plus in-process tool calls.
- Search is keyword-only by design; PDFs are unsupported (text-based PDFs would need an optional `pypdf`
  dependency and page-level sections).

## 6. Questions for Gemini

1. Is the `list[dict]` tool contract in the spec a hard requirement (e.g. another consumer parses it), or was it
   illustrative? The current text output was a deliberate token-efficiency choice, but nothing depends on it yet.
2. Were the tool names (`get_note_outline`, `refresh_index`) chosen for a reason that should win over the current
   names?
3. Should prefix matching (`"term"*`) be restored in the search fallback?
4. Any context on the target agents/clients (e.g. local models via Ollama, Windows hosts) that should change
   defaults such as the 10-second first-search wait, the snippet size or `limit`?
5. Are `.txt` and `.docx` in the default set acceptable, or should the default be Markdown-only?
6. Anything in the discussion preceding the spec that these decisions contradict?

## 7. Outcome of the review

Gemini's answers to the questions in section 6:

| Question | Answer |
|----------|--------|
| 1. Is `list[dict]` a hard requirement? | No, illustrative only. Plain text is better for LLM consumers (fewer tokens). A `--output-format json` flag could be added later if a programmatic consumer appears. |
| 2. Do `get_note_outline` / `refresh_index` have to stay? | No. The shorter names (`get_outline`, `sync_notes`, `list_notes`, `get_links`) are kept. |
| 3. Restore prefix matching? | Yes, in the fallback. |
| 4. Context on target clients | Local models (Ollama/Qwen) have short effective contexts: default `limit` should be 5; snippets stay tight (about 25-35 tokens); the 10 s first-search wait is fine. |
| 5. Default extensions | Keep `.docx` on by default. Keep text formats on, but filter noise. |
| 6. Contradictions with earlier discussion | None. |

Changes made as a result:

| Change | Commit |
|--------|--------|
| Default cache file is `<name>_<sha256(notes_dir)[:8]>.db` (fixes the same-name collision) | `fix: include a hash of the notes directory in the default cache file name` |
| The scan skips symlinked files that resolve outside the notes directory (logged); symlinked directories are not followed | `fix: skip symlinked files that resolve outside the notes directory` |
| The search fallback uses prefix-matched terms (`"term"*`, ANDed) | `feat: prefix-match terms in the search fallback` |
| Default `limit` is 5 | `feat: lower the default search limit to 5` |
| Word `~$` lock files are ignored | already implemented (all extensions); covered by `test_word_lock_files_skipped` |

Not adopted: the suggested fixed skip list (`requirements.txt`, `license.txt`, `todo.txt`, `pom.xml`). `pom.xml` is
not an indexed format, and names like `todo.txt` can be real notes; `--exclude` globs would be the more precise
tool if noise becomes a problem (see architecture, risk R-6).
