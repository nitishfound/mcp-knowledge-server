"""Start the server over stdio (like an AI app would), seed a few notes, and call its tools.

    python examples/demo_client.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
NOTES = [
    ("Postgres connection pooling", "Use PgBouncer in transaction mode; keep max pool size near CPU cores x 2.", ["work", "databases"]),
    ("RAG chunking notes", "Chunk by heading, ~800 chars with 150 overlap. Embed heading + text.", ["ai", "rag"]),
    ("Hiking checklist", "Water, rain jacket, map, headlamp, snacks.", ["personal"]),
]


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        params = StdioServerParameters(command=sys.executable, args=["-m", "notes_mcp", "--db", f"{tmp}/notes.db"], cwd=str(ROOT))
        async with Client(params) as client:
            tools = await client.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))
            for title, content, tags in NOTES:
                await client.call_tool("add_note", {"title": title, "content": content, "tags": tags})

            res = await client.call_tool("search_notes", {"query": "how big should chunks be"})
            for hit in res.structured_content["result"]:
                print(f"  [{hit['id']}] {hit['title']}  score={hit['score']}  {hit['snippet']}")

            print("\nnotes://recent ->")
            print((await client.read_resource("notes://recent")).contents[0].text)

            print("\nlist_tags ->", (await client.call_tool("list_tags", {})).structured_content)
            err = await client.call_tool("get_note", {"note_id": 999})
            print("get_note(999) ->", err.content[0].text, "(is_error =", err.is_error, ")")


if __name__ == "__main__":
    asyncio.run(main())
