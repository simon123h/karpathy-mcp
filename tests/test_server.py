import asyncio

from mcp_markdown.server import build_server, parse_args


def make_server(notes, cache, *extra):
    args = parse_args(["--notes-dir", str(notes), "--cache-dir", str(cache), *extra])
    return build_server(args)


def call(mcp, tool, **arguments):
    return asyncio.run(mcp.call_tool(tool, arguments))[1]["result"]


def hits(text):
    return [block for block in text.split("\n\n") if " — " in block]


def test_search_returns_five_hits_by_default(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    for i in range(8):
        (notes / f"n{i}.md").write_text(f"# Note {i}\nzebra {i}")
    mcp = make_server(notes, tmp_path / "cache")
    assert len(hits(call(mcp, "search_notes", query="zebra"))) == 5
    assert len(hits(call(mcp, "search_notes", query="zebra", limit=8))) == 8


def test_instances_with_the_same_name_get_separate_indexes(tmp_path):
    cache = tmp_path / "cache"
    servers = []
    for name in ("a", "b"):
        notes = tmp_path / name
        notes.mkdir()
        (notes / "only.md").write_text(f"# {name.upper()}\nword{name}")
        servers.append(make_server(notes, cache, "--name", "wiki"))
    assert len(list(cache.glob("wiki_*.db"))) == 2
    assert "only.md" in call(servers[0], "search_notes", query="worda")
    assert "No results" in call(servers[0], "search_notes", query="wordb")
    assert "only.md" in call(servers[1], "search_notes", query="wordb")


def test_max_file_size_option_is_in_megabytes(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "small.md").write_text("# Small\nzebra")
    (notes / "big.md").write_text("# Big\nzebra " + "x" * 3000)
    mcp = make_server(notes, tmp_path / "cache", "--max-file-size", "0.001")  # about 1 KB
    listing = call(mcp, "list_notes")
    assert "small.md" in listing and "big.md" not in listing
    unlimited = make_server(notes, tmp_path / "cache2", "--max-file-size", "0")
    assert "big.md" in call(unlimited, "list_notes")
