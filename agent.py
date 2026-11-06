"""The data question agent: plain-English question in, SQL answer and chart out.

Gemini gets two tools:
  run_sql      runs a read-only query and sees the first rows
  show_result  picks the final query, writes the answer, and picks a chart
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd
from google import genai
from google.genai import errors, types

ENV_PATH = Path(__file__).parent / ".env"
DB_PATH = Path(__file__).parent / "data" / "taxi.duckdb"
# Tried in order if one is busy. Flash-Lite has the largest free daily limit.
MODELS = ["gemini-3.5-flash-lite"]
MAX_TURNS = 8
ROWS_SHOWN_TO_MODEL = 50
MAX_ROWS = 10_000
RATE_LIMIT_RETRIES = 3


def load_env() -> None:
    """Read KEY=value lines from .env so the API key stays out of the code."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name and not name.startswith("#"):
            os.environ.setdefault(name.strip(), value.strip())


load_env()

COLUMN_NOTES = {
    "trips": "One row per yellow taxi trip picked up in January 2024 in New York City.",
    "trips.pickup_location_id": "Join to zones.location_id.",
    "trips.dropoff_location_id": "Join to zones.location_id.",
    "trips.payment_type": "Join to payment_types.payment_type for the name.",
    "trips.trip_distance": "Miles.",
    "trips.fare_amount": "Meter fare in US dollars.",
    "trips.tip_amount": "US dollars. Recorded for credit card tips only; cash tips are not recorded.",
    "trips.total_amount": "Total charged to the rider in US dollars, not including cash tips.",
    "zones": "NYC taxi zones. borough is Manhattan, Brooklyn, Queens, Bronx, Staten Island, EWR, or Unknown.",
    "trips_clean": "Same columns as trips, without the rows any quality rule flags.",
    "quality_rules": "Checks for records that are almost surely errors. columns_affected lists the trips columns each rule touches.",
}

SYSTEM_PROMPT = """You answer questions about New York City taxi trips by writing DuckDB SQL.

Work like an analyst:
1. Use run_sql to check the data when you are unsure of a value or column.
2. Answer from the trips table first.
3. Check data quality. The quality rules are listed below. A rule applies only when your query reads a column the rule affects, in a calculation (like avg(trip_distance)) or a filter (like WHERE passenger_count > 4). Counting rows with count(*), or grouping by zone, borough, vendor, or payment type, reads none of those columns, so no rule applies. When a rule applies, run the same query on trips_clean instead of trips.
4. Call show_result once with the trips query, a short plain-English answer from trips only, and a chart choice. Put clean numbers only in quality_note, never in the answer. If you ran a trips_clean query, also pass it as clean_sql and write quality_note: which rules applied, how many rows they flag, and the clean answer. If the clean answer differs by less than 1%, say the bad records don't change the answer. If no rule applies, leave clean_sql and quality_note empty.

Rules:
- Only read data. Write a single SELECT or WITH query.
- Use the column notes. Join zones for borough or zone names, and payment_types for payment names.
- In the answer, state the number and what it means in one or two sentences. Round dollars to cents.
- Pick a bar chart for categories, a line chart for values over time, and no chart for a single number.
- If the data cannot answer the question, say so in plain text and do not call show_result.

Schema:
{schema}

Quality rules:
{rules}"""

TOOLS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="run_sql",
            description="Run a read-only DuckDB SQL query on the taxi database. Returns the row count and the first 50 rows as CSV.",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A single SELECT or WITH query."},
                },
                "required": ["sql"],
            },
        ),
        types.FunctionDeclaration(
            name="show_result",
            description="Show the final answer to the user. Call this once, with the query that answers the question.",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "The query whose result answers the question."},
                    "answer": {"type": "string", "description": "One or two plain-English sentences."},
                    "chart": {"type": "string", "enum": ["none", "bar", "line"]},
                    "x": {"type": "string", "description": "Column for the x axis, or empty for no chart."},
                    "y": {"type": "string", "description": "Column for the y axis, or empty for no chart."},
                    "clean_sql": {"type": "string", "description": "The same query on trips_clean, or empty if no quality rule applies."},
                    "quality_note": {"type": "string", "description": "Which rules applied, rows flagged, and the clean answer; or empty."},
                },
                "required": ["sql", "answer", "chart", "x", "y", "clean_sql", "quality_note"],
            },
        ),
    ])
]


@dataclass
class Result:
    question: str
    answer: str = ""
    sql: str = ""
    data: pd.DataFrame | None = None
    chart: str = "none"
    x: str = ""
    y: str = ""
    clean_sql: str = ""
    clean_data: pd.DataFrame | None = None
    quality_note: str = ""
    queries_run: list[str] = field(default_factory=list)
    model: str = ""
    error: str = ""


def describe_schema() -> str:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    rows = con.execute("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'main'
        ORDER BY table_name, ordinal_position
    """).fetchall()
    con.close()

    lines = []
    current = None
    for table, column, dtype in rows:
        if table != current:
            current = table
            note = COLUMN_NOTES.get(table, "")
            lines.append(f"\nTable {table}. {note}".rstrip())
        if table == "trips_clean":
            continue  # same columns as trips; listing them twice wastes the model's attention
        note = COLUMN_NOTES.get(f"{table}.{column}", "")
        lines.append(f"  {column} {dtype} {note}".rstrip())
    return "\n".join(lines).strip()


def flagged_columns() -> set[str]:
    """Every trips column that at least one quality rule affects."""
    con = duckdb.connect(str(DB_PATH), read_only=True)
    rows = con.execute("SELECT columns_affected FROM quality_rules").fetchall()
    con.close()
    return {col.strip() for (cols,) in rows for col in cols.split(",")}


def reads_flagged_column(sql: str) -> bool:
    """Code-side check on the model: does this query name a column a quality rule affects?"""
    return any(re.search(rf"\b{re.escape(col)}\b", sql, re.IGNORECASE) for col in flagged_columns())


def describe_rules() -> str:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    rows = con.execute("SELECT rule, columns_affected, rows_flagged FROM quality_rules").fetchall()
    con.close()
    return "\n".join(f"- {rule} (affects {columns}): {flagged:,} rows" for rule, columns, flagged in rows)


def run_query(sql: str) -> pd.DataFrame:
    """Run one read-only query. Raises ValueError for anything but a SELECT or WITH."""
    cleaned = sql.strip().rstrip(";").strip()
    first_word = cleaned.split(None, 1)[0].lower() if cleaned else ""
    if first_word not in ("select", "with"):
        raise ValueError("Only SELECT or WITH queries are allowed.")
    if ";" in cleaned:
        raise ValueError("Send one query at a time.")
    # read_only=True means DuckDB itself refuses any write.
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return con.execute(f"SELECT * FROM ({cleaned}) LIMIT {MAX_ROWS}").fetchdf()
    finally:
        con.close()


def generate(client: genai.Client, contents: list, config: types.GenerateContentConfig, models: list[str]):
    """Call Gemini and return (response, model used).

    A busy model (5xx) hands off to the next one in the list; the last one
    gets retried. The free tier's rate limit (429) means waiting and trying
    the same model again.
    """
    last_error: errors.APIError | None = None
    for model in models:
        is_last = model == models[-1]
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                return client.models.generate_content(model=model, contents=contents, config=config), model
            except errors.APIError as exc:
                last_error = exc
                if (exc.code or 0) >= 500 and not is_last:
                    break
                if (exc.code or 0) >= 500 and attempt < RATE_LIMIT_RETRIES:
                    time.sleep(20 * (attempt + 1))
                    continue
                if exc.code != 429 or attempt == RATE_LIMIT_RETRIES:
                    raise
                time.sleep(20 * (attempt + 1))
    raise last_error


def make_client() -> genai.Client:
    """A client that gives up on a request after 60 seconds and leaves retries to generate()."""
    return genai.Client(http_options=types.HttpOptions(
        timeout=60_000,  # milliseconds
        retry_options=types.HttpRetryOptions(attempts=1),
    ))


def ask(question: str, client: genai.Client | None = None) -> Result:
    client = client or make_client()
    result = Result(question=question)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT.format(schema=describe_schema(), rules=describe_rules()),
        tools=TOOLS,
        # This file runs the tools itself, so turn off the SDK's automatic calling.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents: list[types.Content] = [types.Content(role="user", parts=[types.Part(text=question)])]

    models = MODELS
    for _ in range(MAX_TURNS):
        try:
            response, model = generate(client, contents, config, models)
            # Stay on one model for the whole question; its thought signatures only work with itself.
            models = [model]
            result.model = model
        except errors.APIError as exc:
            result.error = f"Gemini API error {exc.code}: {exc.message}"
            return result

        if not response.candidates or response.candidates[0].content is None:
            result.error = "Gemini returned no answer for this question."
            return result

        # Send the model's turn back unchanged; it carries the thought signatures Gemini needs.
        contents.append(response.candidates[0].content)
        calls = response.function_calls or []

        if not calls:
            if not result.answer:
                result.answer = (response.text or "").strip()
            return result

        parts = []
        for call in calls:
            args = call.args or {}
            sql = args.get("sql", "")
            try:
                data = run_query(sql)
            except Exception as exc:  # DuckDB errors go back to Gemini so it can fix the query.
                parts.append(types.Part(function_response=types.FunctionResponse(
                    id=call.id, name=call.name, response={"error": str(exc)},
                )))
                continue

            result.queries_run.append(sql)
            if call.name == "show_result":
                result.sql = sql
                result.data = data
                result.answer = args.get("answer", "")
                result.chart = args.get("chart", "none")
                result.x = args.get("x", "")
                result.y = args.get("y", "")
                result.quality_note = args.get("quality_note", "")
                clean_sql = args.get("clean_sql", "").strip()
                if not clean_sql or not reads_flagged_column(sql):
                    # No clean query, or the main query reads no flagged column: no rule applies.
                    result.quality_note = ""
                else:
                    try:
                        result.clean_data = run_query(clean_sql)
                        result.clean_sql = clean_sql
                    except Exception:  # a bad clean query shouldn't hide a good answer
                        result.quality_note = ""
                output = "Shown to the user."
            else:
                preview = data.head(ROWS_SHOWN_TO_MODEL).to_csv(index=False)
                output = f"{len(data)} rows.\n{preview}"
            parts.append(types.Part(function_response=types.FunctionResponse(
                id=call.id, name=call.name, response={"output": output},
            )))

        contents.append(types.Content(role="user", parts=parts))
        if result.sql:
            return result

    result.error = f"Stopped after {MAX_TURNS} turns without a final answer."
    return result


if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]) or "Which borough had the most pickups?"
    r = ask(question)
    print(r.error or r.answer)
    if r.sql:
        print("\nSQL:\n" + r.sql)
        print(r.data.head(10).to_string(index=False))
