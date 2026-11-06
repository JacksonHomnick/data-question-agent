"""Tests for the MCP server: starts it over stdio like a real client would.

Needs the database (python setup_data.py). Makes no Gemini calls: ask_question isn't called.
Run:  python -m unittest discover tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
SERVER = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")], cwd=str(ROOT))


async def with_session(fn):
    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def text(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


@unittest.skipUnless((ROOT / "data" / "taxi.duckdb").exists(), "run python setup_data.py first")
class McpServerTest(unittest.TestCase):
    def call(self, fn):
        return anyio.run(with_session, fn)

    def test_lists_three_read_only_tools(self):
        tools = self.call(lambda s: s.list_tools()).tools
        self.assertEqual({t.name for t in tools}, {"ask_question", "run_sql", "describe_database"})
        self.assertTrue(all(t.annotations.read_only_hint is True for t in tools))

    def test_run_sql_returns_rows(self):
        out = text(self.call(lambda s: s.call_tool("run_sql", {"sql": "SELECT count(*) AS trips FROM trips"})))
        self.assertIn("1 rows.", out)
        self.assertIn("2964606", out)

    def test_run_sql_refuses_writes(self):
        out = text(self.call(lambda s: s.call_tool("run_sql", {"sql": "DELETE FROM trips"})))
        self.assertIn("Only SELECT or WITH queries are allowed.", out)

    def test_describe_database_includes_rules(self):
        out = text(self.call(lambda s: s.call_tool("describe_database", {})))
        self.assertIn("trip_distance", out)
        self.assertIn("Distance over 100 miles", out)


if __name__ == "__main__":
    unittest.main()
