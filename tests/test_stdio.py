"""Start the real server as a subprocess and talk to it over stdio, like Claude Desktop does."""

import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]


async def test_stdio_end_to_end(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "notes_mcp", "--db", str(tmp_path / "notes.db")],
        cwd=str(ROOT),
    )
    async with Client(params) as client:
        created = await client.call_tool("add_note", {"title": "Hello", "content": "stdio works", "tags": ["test"]})
        assert not created.is_error
        hits = await client.call_tool("search_notes", {"query": "stdio"})
        assert hits.structured_content["result"][0]["title"] == "Hello"
