"""Slack front end for the data question agent.

@mention the bot with a question; it replies in the thread with the answer,
the SQL, a preview of the result, and any data-quality warning.

Run:  python slack_bot.py
Needs SLACK_BOT_TOKEN (xoxb-...) and SLACK_APP_TOKEN (xapp-...) in .env. See the README.
"""

from __future__ import annotations

import os
import re
import ssl

import certifi
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from agent import Result, ask, make_client  # importing agent also loads .env

PREVIEW_ROWS = 10
MENTION = re.compile(r"<@[A-Z0-9]+>")


def clean_question(text: str) -> str:
    """Drop the bot's @mention so only the question goes to the agent."""
    return MENTION.sub("", text).strip()


def format_reply(result: Result) -> str:
    """Turn an agent Result into one Slack message (Slack's mrkdwn format)."""
    if result.error:
        return f":x: {result.error}"

    parts = [result.answer or "I couldn't find an answer to that."]

    if result.quality_note:
        parts.append(f":warning: *Data quality:* {result.quality_note}")

    if result.data is not None and not result.data.empty:
        preview = result.data.head(PREVIEW_ROWS).to_string(index=False)
        more = f"\n_Showing {PREVIEW_ROWS} of {len(result.data):,} rows._" if len(result.data) > PREVIEW_ROWS else ""
        parts.append(f"```{preview}```{more}")

    if result.sql:
        parts.append(f"*SQL*\n```{result.sql}```")

    return "\n\n".join(parts)


def respond(event: dict, say, update, ask_fn) -> None:
    """Answer one @mention. say posts in the thread, update edits a message, ask_fn runs the agent."""
    question = clean_question(event.get("text", ""))
    thread = event.get("thread_ts") or event["ts"]
    if not question:
        say(text="Ask me a question about NYC taxi trips, like: Which borough had the most pickups?", thread_ts=thread)
        return

    # Post a placeholder right away, then replace it, since answers take a few seconds.
    placeholder = say(text=":hourglass_flowing_sand: Writing and running SQL...", thread_ts=thread)
    try:
        reply = format_reply(ask_fn(question))
    except Exception as exc:  # never leave the placeholder hanging
        reply = f":x: Something went wrong: {exc}"
    update(channel=event["channel"], ts=placeholder["ts"], text=reply)


def build_app() -> App:
    # python.org's Python can't find the Mac's certificates, so use certifi's.
    # The Socket Mode connection reuses this client's SSL settings.
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    app = App(client=WebClient(token=os.environ["SLACK_BOT_TOKEN"], ssl=ssl_context))
    gemini = make_client()

    # Bolt fills in these arguments by name: client is Slack's API client.
    @app.event("app_mention")
    def handle_mention(event, say, client):
        respond(event, say, client.chat_update, lambda question: ask(question, client=gemini, source="slack"))

    return app


def main() -> None:
    missing = [name for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "GEMINI_API_KEY") if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)}. See the README's Slack section.")
    print("Connecting to Slack. Press Ctrl+C to stop.")
    SocketModeHandler(build_app(), os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
