"""Web page for the data question agent.

Run:  streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from agent import DB_PATH, ask

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

if not DB_PATH.exists():
    st.error("No database yet. Run `python setup_data.py` first.")
    st.stop()

if "question" not in st.session_state:
    st.session_state.question = ""

cols = st.columns(2)
for i, example in enumerate(EXAMPLES):
    if cols[i % 2].button(example, use_container_width=True):
        st.session_state.question = example

question = st.text_input("Your question", key="question")

if st.button("Ask", type="primary") and question.strip():
    with st.spinner("Writing and running SQL..."):
        result = ask(question.strip())

    if result.error:
        st.error(result.error)
    else:
        st.subheader("Answer")
        st.write(result.answer)

        data = result.data
        if data is not None and result.chart != "none" and result.x in data.columns and result.y in data.columns:
            if result.chart == "line":
                st.line_chart(data, x=result.x, y=result.y)
            else:
                st.bar_chart(data, x=result.x, y=result.y)

        if data is not None:
            st.dataframe(data, use_container_width=True, hide_index=True)

        if result.quality_note:
            st.subheader("Data quality")
            st.warning(result.quality_note)
            if result.clean_data is not None:
                with st.expander("Answer without flagged records"):
                    st.dataframe(result.clean_data, use_container_width=True, hide_index=True)
                    st.code(result.clean_sql, language="sql")

        if result.sql:
            with st.expander("SQL"):
                st.code(result.sql, language="sql")
