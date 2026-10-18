"""End-to-end tests: run the real server as a subprocess and talk MCP over stdio."""

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SRC = Path(__file__).resolve().parents[1] / "src"
TIMEOUT = 60

EXPECTED_TOOLS = {
    "search_notes",
    "read_note",
    "get_outline",
    "list_notes",
    "get_links",
    "sync_notes",
}


@pytest.fixture
def notes(tmp_path):
    d = tmp_path / "notes"
    (d / "sub").mkdir(parents=True)
    (d / "guide.md").write_text(
        "# Zebra Guide\nintro, see [[other]]\n\n## Setup\nrun the zebra pipeline nightly\n",
        encoding="utf-8",
    )
    (d / "sub" / "other.md").write_text("# Other\nback to [[guide]], café\n", encoding="utf-8")
    return d


def server_command(notes, cache):
    return [
        sys.executable,
        "-m",
        "mcp_markdown",
        "--notes-dir",
        str(notes),
        "--cache-dir",
        str(cache),
        "--name",
        "e2e",
        "--description",
        "End-to-end test notes",
    ]


def server_env():
    """The child process must import the package from src/ even if it is not installed."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH")]))
    return env


def text_of(result):
    assert not result.isError, result
    return "\n".join(c.text for c in result.content if c.type == "text")


async def session_scenario(notes, cache):
    command = server_command(notes, cache)
    params = StdioServerParameters(command=command[0], args=command[1:], env=server_env())
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.serverInfo.name == "e2e"
        assert "End-to-end test notes" in init.instructions
        assert "search_notes" in init.instructions

        tools = await session.list_tools()
        assert {t.name for t in tools.tools} == EXPECTED_TOOLS

        found = text_of(await session.call_tool("search_notes", {"query": "zebra pipeline"}))
        assert "guide.md — Zebra Guide > Setup" in found

        # the section handle printed by a hit can be read back
        section = text_of(
            await session.call_tool("read_note", {"path": "guide.md", "section": "Setup"})
        )
        assert section.startswith("## Setup") and "nightly" in section

        outline = text_of(await session.call_tool("get_outline", {"path": "guide.md"}))
        assert "# Zebra Guide" in outline and "## Setup" in outline

        links = text_of(await session.call_tool("get_links", {"path": "guide.md"}))
        assert "sub/other.md" in links

        listing = text_of(await session.call_tool("list_notes", {}))
        assert "guide.md — Zebra Guide" in listing and "sub/other.md — Other" in listing

        # diacritics are folded and malformed FTS5 syntax falls back to prefix terms
        assert "sub/other.md" in text_of(await session.call_tool("search_notes", {"query": "cafe"}))
        assert "guide.md" in text_of(await session.call_tool("search_notes", {"query": "zebr:"}))

        # a new note becomes searchable after an explicit sync
        (notes / "late.md").write_text("# Late\nkangaroo\n", encoding="utf-8")
        synced = text_of(await session.call_tool("sync_notes", {}))
        assert "1 added" in synced
        assert "late.md" in text_of(await session.call_tool("search_notes", {"query": "kangaroo"}))

        # path traversal is reported as an error text, not an exception or a file read
        escape = text_of(await session.call_tool("read_note", {"path": "../../etc/passwd"}))
        assert escape.startswith("Error:") and "escapes" in escape


def test_full_mcp_session_over_stdio(notes, tmp_path):
    asyncio.run(asyncio.wait_for(session_scenario(notes, tmp_path / "cache"), TIMEOUT))


def test_stdout_carries_only_json_rpc(notes, tmp_path):
    """Anything else on stdout (a stray print, a log line) would corrupt the protocol."""
    proc = subprocess.Popen(
        server_command(notes, tmp_path / "cache"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        env=server_env(),
    )
    out_lines = queue.Queue()
    reader = threading.Thread(target=lambda: [out_lines.put(line) for line in proc.stdout])
    reader.daemon = True
    reader.start()

    def send(message):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        proc.stdin.flush()

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "stdout-test", "version": "0"},
                },
            }
        )
        assert json.loads(out_lines.get(timeout=TIMEOUT))["id"] == 1
        send({"method": "notifications/initialized"})
        send({"id": 2, "method": "tools/list"})
        tools = json.loads(out_lines.get(timeout=TIMEOUT))
        assert {t["name"] for t in tools["result"]["tools"]} == EXPECTED_TOOLS
        send(
            {
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search_notes", "arguments": {"query": "zebra"}},
            }
        )
        assert json.loads(out_lines.get(timeout=TIMEOUT))["id"] == 3
        proc.stdin.close()
        proc.wait(timeout=TIMEOUT)
        reader.join(timeout=TIMEOUT)
        stderr = proc.stderr.read()
    finally:
        proc.kill()
        proc.stderr.close()
        proc.stdout.close()

    leftovers = []
    while not out_lines.empty():
        leftovers.append(out_lines.get())
    for line in leftovers:
        assert json.loads(line)["jsonrpc"] == "2.0"  # raises on any non-protocol output
    assert "sync:" in stderr  # logging goes to stderr instead


def test_missing_notes_dir_exits_with_an_error(tmp_path):
    result = subprocess.run(
        server_command(tmp_path / "does-not-exist", tmp_path / "cache"),
        capture_output=True,
        encoding="utf-8",
        env=server_env(),
        timeout=TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode != 0
    assert "not a directory" in result.stderr
    assert result.stdout == ""
