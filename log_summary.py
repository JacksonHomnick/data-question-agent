"""Summarize logs/queries.jsonl: how often the agent answers, how fast, and what it costs.

Run:  python log_summary.py
"""

from __future__ import annotations

import pandas as pd

from agent import LOG_PATH


def main() -> None:
    if not LOG_PATH.exists():
        raise SystemExit("No log yet. Ask a question first.")
    log = pd.read_json(LOG_PATH, lines=True)
    log["used_examples"] = log["examples_used"].str.len() > 0
    summary = log.groupby("source").agg(
        questions=("question", "size"),
        answered=("ok", "mean"),
        median_seconds=("seconds", "median"),
        median_tokens=("tokens", "median"),
        failed_queries=("failed_queries", "sum"),
        quality_flags=("quality_flag", "sum"),
        used_examples=("used_examples", "mean"),
    )
    summary["answered"] = (summary["answered"] * 100).round(0).astype(int).astype(str) + "%"
    summary["used_examples"] = (summary["used_examples"] * 100).round(0).astype(int).astype(str) + "%"
    print(summary.to_string())
    errors = log[log["error"] != ""]
    if not errors.empty:
        print("\nLast errors:")
        print(errors[["at", "source", "error"]].tail(5).to_string(index=False))


if __name__ == "__main__":
    main()
