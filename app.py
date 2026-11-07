"""Web page for the data question agent.

Run:  streamlit run app.py

Hosted on Streamlit Community Cloud, it reads its settings from the app's
secrets instead of .env, builds the database on first start, and can
require a password and cap questions per day to protect the free API quota.
"""

from __future__ import annotations

import hmac
import os
import re

import streamlit as st

# On Streamlit Community Cloud, settings come from the app's secrets; copy them into the environment.
try:
    for name in ("GEMINI_API_KEY", "APP_PASSWORD", "DAILY_QUESTION_LIMIT"):
        if name in st.secrets:
            os.environ.setdefault(name, str(st.secrets[name]))
except Exception:  # no secrets file when running locally
    pass

from agent import DB_PATH, ask, questions_logged_today  # noqa: E402  importing agent also loads .env

DAILY_QUESTION_LIMIT = int(os.environ.get("DAILY_QUESTION_LIMIT", "50"))

def plain_markdown(text: str) -> str:
    """Model text for a box that renders markdown: escape every symbol and break web addresses,
    so a tricked model can't show links, images, or math formatting."""
    text = re.sub(r"([!-/:-@\[-`{-~])", r"\\\1", text)
    return text.replace("\\:\\/\\/", "\\:\u200b\\/\\/").replace("www\\.", "www\u200b\\.")


EXAMPLES = [
    "Which borough had the most pickups?",
    "How many trips were there each day?",
    "What share of trips were paid by credit card?",
    "Which 10 pickup zones had the highest average tip?",
    "What was the longest trip distance recorded?",
]

st.set_page_config(page_title="Data Question Agent", page_icon="📊")
st.title("Data Question Agent")
st.caption("Ask a question about NYC yellow taxi trips from January 2024. Gemini writes the SQL, runs it, and answers.")

if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
    st.error("No Gemini API key found. Save it in a `.env` file as `GEMINI_API_KEY=...` (see the README), then reload.")
    st.stop()

password = os.environ.get("APP_PASSWORD", "")
if password and not st.session_state.get("signed_in"):
    entered = st.text_input("Password", type="password")
    if not entered:
        st.stop()
    if not hmac.compare_digest(entered, password):
        st.error("Wrong password.")
        st.stop()
    st.session_state.signed_in = True
    st.rerun()


@st.cache_resource(show_spinner=False)
def build_database_once() -> None:
    """A fresh cloud server has no data/ folder, so download and build it once."""
    import setup_data

    setup_data.download()
    setup_data.build_database()


if not DB_PATH.exists():
    with st.spinner("First start: downloading 3 million taxi trips and building the database (about a minute)..."):
        build_database_once()

if "question" not in st.session_state:
    st.session_state.question = ""

cols = st.columns(2)
for i, example in enumerate(EXAMPLES):
    if cols[i % 2].button(example, width="stretch"):
        st.session_state.question = example

question = st.text_input("Your question", key="question")

if st.button("Ask", type="primary") and question.strip():
    if questions_logged_today("web") >= DAILY_QUESTION_LIMIT:
        st.error(f"This demo has hit its limit of {DAILY_QUESTION_LIMIT} questions today. Try again tomorrow.")
        st.stop()
    with st.spinner("Writing and running SQL..."):
        result = ask(question.strip(), source="web")

    if result.error:
        st.error(result.error)
    else:
        st.subheader("Answer")
        st.text(result.answer)  # plain text: the model's words never render as links or images

        data = result.data
        if data is not None and result.chart != "none" and result.x in data.columns and result.y in data.columns:
            if result.chart == "line":
                st.line_chart(data, x=result.x, y=result.y)
            else:
                st.bar_chart(data, x=result.x, y=result.y)

        if data is not None:
            st.dataframe(data, width="stretch", hide_index=True)

        if result.quality_note:
            st.subheader("Data quality")
            st.warning(plain_markdown(result.quality_note))
            if result.clean_data is not None:
                with st.expander("Answer without flagged records"):
                    st.dataframe(result.clean_data, width="stretch", hide_index=True)
                    st.code(result.clean_sql, language="sql")

        if result.sql:
            with st.expander("SQL"):
                st.code(result.sql, language="sql")
