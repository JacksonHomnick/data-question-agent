"""Score the agent on 20 questions with known answers.

Each question has a reference query. The agent passes when the expected value
shows up in its result table or in its answer text (numbers within 1%).

Run:  python run_evals.py
Each run calls Gemini about 40 to 80 times. That fits the free tier, but a
second run on the same day may hit the daily limit.
"""

from __future__ import annotations

import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path

from agent import ask, make_client, run_query

HERE = Path(__file__).parent
QUESTIONS = HERE / "evals" / "questions.json"
RESULTS_DIR = HERE / "evals" / "results"
TOLERANCE = 0.01
PAUSE_SECONDS = 5  # spaces out calls to stay under the free tier's per-minute limit
NUMBER = re.compile(r"-?\d[\d,]*\.?\d*")
WRITTEN_DATE = re.compile(r"[A-Z][a-z]+ \d{1,2},? \d{4}")


def candidates(result) -> tuple[list[float], list[str]]:
    """Numbers and strings the agent produced: first rows of its table plus its answer text."""
    numbers: list[float] = []
    texts: list[str] = [result.answer.lower()]
    if result.data is not None:
        for row in result.data.head(5).itertuples(index=False):
            for cell in row:
                texts.append(str(cell).lower())
                try:
                    numbers.append(float(cell))
                except (TypeError, ValueError):
                    pass
    # Turn written dates like "January 27, 2024" into 2024-01-27 so they match the reference.
    for match in WRITTEN_DATE.findall(result.answer):
        try:
            texts.append(datetime.strptime(match.replace(",", ""), "%B %d %Y").strftime("%Y-%m-%d"))
        except ValueError:
            pass
    for match in NUMBER.findall(result.answer):
        try:
            numbers.append(float(match.replace(",", "")))
        except ValueError:
            pass
    return numbers, texts


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(TOLERANCE * abs(b), 0.01)


def passes(case: dict, expected, result) -> bool:
    numbers, texts = candidates(result)
    if case["kind"] == "number":
        targets = [float(expected)]
        if case.get("percent"):
            targets.append(float(expected) / 100)  # accept 0.72 as well as 72%
        return any(close(n, t) for n in numbers for t in targets)
    want = str(expected).lower()
    return any(want in t for t in texts)


def quality_check(case: dict, result) -> bool | None:
    """None when the case has no quality expectation.

    A flag is expected: the agent must flag it and its clean query must return the clean reference value.
    No flag is expected: the agent must not flag it.
    """
    if "expect_flag" not in case or result.error:
        return None if "expect_flag" not in case else False
    flagged = bool(result.clean_sql)
    if not case["expect_flag"]:
        return not flagged
    if not flagged or result.clean_data is None:
        return False
    clean_expected = float(run_query(case["clean_reference_sql"]).iloc[0, 0])
    cells = []
    for row in result.clean_data.head(5).itertuples(index=False):
        for cell in row:
            try:
                cells.append(float(cell))
            except (TypeError, ValueError):
                pass
    return any(close(c, clean_expected) for c in cells)


def main() -> None:
    cases = json.loads(QUESTIONS.read_text())
    client = make_client()
    rows = []
    passed = 0
    quality_cases = 0
    quality_passed = 0

    for i, case in enumerate(cases):
        if i:
            time.sleep(PAUSE_SECONDS)
        expected = run_query(case["reference_sql"]).iloc[0, 0]
        result = ask(case["question"], client=client)
        ok = not result.error and passes(case, expected, result)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {case['id']:>2}. {case['question']}")
        if not ok:
            print(f"        expected {expected}; got: {result.error or result.answer}")

        quality = quality_check(case, result)
        if quality is not None:
            quality_cases += 1
            quality_passed += quality
            if not quality:
                want = "a quality flag with the clean answer" if case["expect_flag"] else "no quality flag"
                print(f"        quality: expected {want}; got: {result.quality_note or 'no flag'}")

        rows.append({
            "id": case["id"],
            "question": case["question"],
            "expected": expected,
            "answer": result.error or result.answer,
            "sql": result.sql,
            "quality_note": result.quality_note,
            "clean_sql": result.clean_sql,
            "model": result.model,
            "passed": ok,
            "quality_passed": "" if quality is None else quality,
        })

    print(f"\nScore: {passed}/{len(cases)}")
    print(f"Quality checks: {quality_passed}/{quality_cases}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now():%Y-%m-%d_%H%M}.csv"
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved {out.relative_to(HERE)}")


if __name__ == "__main__":
    main()
