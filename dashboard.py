from __future__ import annotations

import sqlite3
from pathlib import Path

from config import load_config


def load_rows(database_path: Path) -> list[dict]:
    if not database_path.exists():
        return []
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM listings ORDER BY first_seen_at DESC").fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def main() -> None:
    import streamlit as st
    import pandas as pd

    config = load_config()
    rows = load_rows(config.database_path)
    frame = pd.DataFrame(rows)

    st.set_page_config(page_title="Lebanon Owner Leads", layout="wide")
    st.title("Lebanon Owner Leads")

    if frame.empty:
        st.info("No leads saved yet. Run: python main.py --dry-run")
        return

    likely = frame[frame["lead_type"] == "likely_owner"]
    today = frame[frame["first_seen_at"].astype(str).str.startswith(pd.Timestamp.now().strftime("%Y-%m-%d"))]

    col1, col2, col3 = st.columns(3)
    col1.metric("Total leads", len(frame))
    col2.metric("New today", len(today))
    col3.metric("Likely owners", len(likely))

    areas = ["All", *sorted(frame["area"].dropna().unique())]
    lead_types = ["All", *sorted(frame["lead_type"].dropna().unique())]
    area = st.selectbox("Area", areas)
    lead_type = st.selectbox("Lead type", lead_types)
    query = st.text_input("Search")

    filtered = frame.copy()
    if area != "All":
        filtered = filtered[filtered["area"] == area]
    if lead_type != "All":
        filtered = filtered[filtered["lead_type"] == lead_type]
    if query:
        text = filtered.fillna("").astype(str).agg(" ".join, axis=1)
        filtered = filtered[text.str.contains(query, case=False, regex=False)]

    st.bar_chart(frame.groupby("area").size().sort_values(ascending=False))
    st.dataframe(filtered, use_container_width=True, hide_index=True)
    st.download_button(
        "Export CSV",
        filtered.to_csv(index=False).encode("utf-8-sig"),
        "owner_leads_filtered.csv",
        "text/csv",
    )


if __name__ == "__main__":
    main()
