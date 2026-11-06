"""Tests for the Slack front end. No Slack or Gemini calls: both sides are fakes.

Run:  python -m unittest discover tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import Result  # noqa: E402
from slack_bot import clean_question, format_reply, respond  # noqa: E402


class FakeSlack:
    """Records what the bot posts and edits."""

    def __init__(self):
        self.posts = []
        self.updates = []

    def say(self, text, thread_ts):
        self.posts.append({"text": text, "thread_ts": thread_ts})
        return {"ts": "placeholder-ts"}

    def update(self, channel, ts, text):
        self.updates.append({"channel": channel, "ts": ts, "text": text})


EVENT = {"text": "<@U0BOT123> Which borough had the most pickups?", "ts": "111.222", "channel": "C123"}


class CleanQuestionTest(unittest.TestCase):
    def test_removes_mention(self):
        self.assertEqual(clean_question(EVENT["text"]), "Which borough had the most pickups?")

    def test_only_mention_is_empty(self):
        self.assertEqual(clean_question("<@U0BOT123>  "), "")


class FormatReplyTest(unittest.TestCase):
    def test_answer_table_and_sql(self):
        data = pd.DataFrame({"borough": ["Manhattan", "Queens"], "trips": [2646933, 273125]})
        reply = format_reply(Result(question="q", answer="Manhattan had the most.", sql="SELECT 1", data=data))
        self.assertTrue(reply.startswith("Manhattan had the most."))
        self.assertIn("Manhattan", reply.split("```")[1])  # table preview in a code block
        self.assertIn("*SQL*\n```SELECT 1```", reply)
        self.assertNotIn("Data quality", reply)

    def test_quality_note_shown(self):
        reply = format_reply(Result(question="q", answer="312,722.3 miles.", quality_note="Clean answer: 97.5 miles."))
        self.assertIn(":warning: *Data quality:* Clean answer: 97.5 miles.", reply)

    def test_long_result_says_how_many_rows(self):
        data = pd.DataFrame({"day": range(31), "trips": range(31)})
        reply = format_reply(Result(question="q", answer="Daily trips.", data=data))
        self.assertIn("Showing 10 of 31 rows.", reply)

    def test_error(self):
        self.assertEqual(format_reply(Result(question="q", error="Gemini API error 503")), ":x: Gemini API error 503")


class RespondTest(unittest.TestCase):
    def test_posts_placeholder_then_answer_in_thread(self):
        slack = FakeSlack()
        asked = []

        def fake_ask(question):
            asked.append(question)
            return Result(question=question, answer="Manhattan had the most pickups.")

        respond(EVENT, slack.say, slack.update, fake_ask)
        self.assertEqual(asked, ["Which borough had the most pickups?"])
        self.assertEqual(slack.posts[0]["thread_ts"], "111.222")
        self.assertEqual(slack.updates[0]["ts"], "placeholder-ts")
        self.assertIn("Manhattan had the most pickups.", slack.updates[0]["text"])

    def test_reply_stays_in_existing_thread(self):
        slack = FakeSlack()
        respond({**EVENT, "thread_ts": "100.000"}, slack.say, slack.update, lambda q: Result(question=q, answer="ok"))
        self.assertEqual(slack.posts[0]["thread_ts"], "100.000")

    def test_empty_question_gets_help_and_skips_agent(self):
        slack = FakeSlack()
        respond({**EVENT, "text": "<@U0BOT123>"}, slack.say, slack.update, lambda q: self.fail("agent should not run"))
        self.assertIn("Ask me a question", slack.posts[0]["text"])
        self.assertEqual(slack.updates, [])

    def test_agent_crash_replaces_placeholder(self):
        slack = FakeSlack()

        def broken_ask(question):
            raise RuntimeError("database locked")

        respond(EVENT, slack.say, slack.update, broken_ask)
        self.assertIn("Something went wrong: database locked", slack.updates[0]["text"])


if __name__ == "__main__":
    unittest.main()
