"""MCP server for the data question agent.

Any MCP client (Claude Desktop, Claude Code, Cursor, and others) can use
these tools over stdio:
  ask_question       the full agent: plain English in, answer, SQL, and quality note out
  run_sql            one read-only DuckDB query, first rows back as CSV
  describe_database  tables, columns, and quality rules

Run:  python mcp_server.py   (the client starts it; see the README)
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from agent import ROWS_SHOWN_TO_MODEL, ask, describe_rules, describe_schema, make_client, run_query

server = MCPServer(
    name="data-question-agent",
    instructions="Answers questions about NYC yellow taxi trips from January 2024. "
    "Use describe_database first if you need column names. All tools are read-only.",
)
READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
_gemini = None


@server.tool(annotations=READ_ONLY)
def ask_question(question: str) -> dict:
    """Answer a plain-English question about the taxi data. Gemini writes and runs the SQL,
    checks data-quality rules, and returns the answer, the SQL, the first rows, and any quality note."""
    global _gemini
    _gemini = _gemini or make_client()
    result = ask(question, client=_gemini, source="mcp")
    if result.error:
        return {"error": result.error}
    return {
        "answer": result.answer,
        "sql": result.sql,
        "rows": [] if result.data is None else result.data.head(ROWS_SHOWN_TO_MODEL).to_dict("records"),
        "quality_note": result.quality_note,
        "clean_sql": result.clean_sql,
    }


@server.tool(annotations=READ_ONLY)
def run_sql(sql: str) -> str:
    """Run one read-only DuckDB query (SELECT or WITH) on the taxi database. Returns the row count and the first 50 rows as CSV."""
    try:
        data = run_query(sql)
    except Exception as exc:  # the client sees the DuckDB error and can fix its query
        return f"Error: {exc}"
    return f"{len(data)} rows.\n{data.head(ROWS_SHOWN_TO_MODEL).to_csv(index=False)}"


@server.tool(annotations=READ_ONLY)
def describe_database() -> str:
    """The tables and columns, with notes, plus the data-quality rules and how many trips each flags."""
    return f"{describe_schema()}\n\nQuality rules:\n{describe_rules()}"


if __name__ == "__main__":
    server.run("stdio")
