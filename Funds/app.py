"""
Funds — real-time NAV dashboard.

Reads every .xlsx in `data/`, parses each fund, enriches with sector/country,
and re-prices holdings live via yfinance. Auto-refreshes every 60 s.

Run locally:
    streamlit run app.py
"""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import parser as fund_parser
from enrichment import enrich_fund
import pricing


DATA_DIR = Path(__file__).parent / "data"
REFRESH_SECONDS = 60
BGN_PER_EUR = 1.95583  # fixed peg (display only)


# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Funds Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Auto-refresh every 60 s — meta tag is the most portable approach across
# Streamlit Cloud / Render / local without a websocket trick.
st.markdown(
    f"<meta http-equiv='refresh' content='{REFRESH_SECONDS}'>",
    unsafe_allow_html=True,
)

# A bit of CSS polish: tighter spacing, monospace numbers, nicer card look.
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.6rem; padding-bottom: 2rem; }
      h1 { font-size: 1.9rem !important; margin-bottom: 0.2rem; }
      h3 { font-size: 1.05rem !important; margin-top: 0.4rem !important;
           color: #aab4c0; font-weight: 600; letter-spacing: 0.02em; }
      .fund-subtitle { color: #8a96a3; font-size: 0.92rem; margin-top: 0; margin-bottom: 1.0rem; }
      .stMetric { background: #161b24; padding: 12px 16px; border-radius: 10px;
                  border: 1px solid #232a36; }
      .stMetric label { color: #8a96a3 !important; font-size: 0.8rem !important; }
      [data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }
      [data-testid="stMetricDelta"] { font-variant-numeric: tabular-nums; }
      .stDataFrame { font-variant-numeric: tabular-nums; }
      div[data-testid="stHorizontalBlock"] { gap: 1rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Data loading (cached on file mtimes)
# ---------------------------------------------------------------------------
def _data_signature(folder: Path) -> tuple:
    return tuple(sorted(
        (p.name, p.stat().st_mtime_ns)
        for p in folder.glob("*.xlsx")
        if not p.name.startswith("~$")
    ))


@st.cache_data(show_spinner=False)
def load_funds(signature: tuple):
    funds = fund_parser.parse_data_dir(DATA_DIR)
    for f in funds:
        enrich_fund(f)
    return funds


# ---------------------------------------------------------------------------
# Live re-pricing
# ---------------------------------------------------------------------------
def reprice_fund(fund) -> dict:
    eurusd = pricing.get_eurusd()
    book_usd_eur = fund.fx_rates.get("USD/EUR", 0.92)
    usd_eur = pricing.usd_to_eur(eurusd, fallback_usd_eur=book_usd_eur)

    rows = []
    live_holdings_total = 0.0
    prev_holdings_total = 0.0
    stale_count = 0

    for h in fund.holdings:
        live_mv = h.market_value
        prev_mv = h.market_value
        live_price = h.price_dirty
        prev_price = h.price_dirty
        is_live = False
        note = ""
        ret_pct = 0.0  # daily return % for this holding (live vs prev close)

        q = pricing.get_quote(h.ticker) if h.ticker else None

        if q and q.ok and q.last and q.prev and h.quantity:
            ccy = (h.currency or "USD").upper()
            if ccy == "USD":
                live_mv = h.quantity * q.last * usd_eur
                prev_mv = h.quantity * q.prev * usd_eur
            elif ccy == "EUR":
                live_mv = h.quantity * q.last
                prev_mv = h.quantity * q.prev
            elif ccy == "BGN":
                live_mv = h.quantity * q.last / BGN_PER_EUR
                prev_mv = h.quantity * q.prev / BGN_PER_EUR
            else:
                ratio = q.last / q.prev if q.prev else 1.0
                live_mv = h.market_value * ratio
                prev_mv = h.market_value
            live_price = q.last
            prev_price = q.prev
            is_live = True
            ret_pct = (q.last / q.prev - 1) * 100 if q.prev else 0.0
        else:
            stale_count += 1
            note = (q.error if q else "no ticker") or "no live data"

        live_holdings_total += live_mv
        prev_holdings_total += prev_mv

        rows.append({
            "Asset class": h.asset_class,
            "Security": h.security_name,
            "Ticker": h.ticker or "",
            "ISIN": h.isin or "",
            "Sector": h.sector or "Unknown",
            "Country": h.country or "Unknown",
            "Currency": h.currency or "",
            "Quantity": h.quantity,
            "Price (book)": h.price_dirty,
            "Price (live)": live_price,
            "MV (EUR)": live_mv,
            "Δ (EUR)": live_mv - prev_mv,
            "Δ (%)": ret_pct,                   # daily return for this holding
            "Weight": 0.0,                      # filled below
            "Live": "✓" if is_live else "—",
            "Note": note,
        })

    book_holdings_total = sum(h.market_value for h in fund.holdings)
    other_assets = (fund.total_assets or (book_holdings_total + sum(c.market_value for c in fund.cash))) - book_holdings_total
    cash_total = sum(c.market_value for c in fund.cash)
    liabilities = fund.total_liabilities

    live_nav = live_holdings_total + other_assets - liabilities
    prev_nav = prev_holdings_total + other_assets - liabilities

    # Weight each holding as a fraction of total live assets.
    live_total_assets = live_holdings_total + other_assets
    for r in rows:
        r["Weight"] = (r["MV (EUR)"] / live_total_assets) if live_total_assets else 0.0

    df = pd.DataFrame(rows)

    # NAV % change via SUMPRODUCT(weight × daily_return).
    # Cash and other non-traded assets contribute 0 (no daily return).
    if not df.empty:
        nav_change_pct = float((df["Weight"] * df["Δ (%)"]).sum())
    else:
        nav_change_pct = 0.0

    units = fund.units_outstanding or 1
    return {
        "fund": fund,
        "live_nav": live_nav,
        "prev_nav": prev_nav,
        "live_nav_per_unit": live_nav / units,
        "prev_nav_per_unit": prev_nav / units,
        "change_abs": live_nav - prev_nav,
        "change_pct": nav_change_pct,                 # SUMPRODUCT-based
        "change_per_unit_abs": (live_nav - prev_nav) / units,
        "holdings_df": df,
        "live_holdings_total": live_holdings_total,
        "other_assets": other_assets,
        "liabilities": liabilities,
        "cash_total": cash_total,
        "fx_used": usd_eur,
        "fx_book": book_usd_eur,
        "eurusd": eurusd,
        "stale_count": stale_count,
    }


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------
def render_title(fund) -> None:
    st.markdown(f"# {fund.fund_name}")
    st.markdown(
        f"<div class='fund-subtitle'>"
        f"Reporting date <b>{fund.reporting_date.isoformat()}</b> · "
        f"Live re-pricing via yfinance · auto-refresh {REFRESH_SECONDS}s · "
        f"last tick {datetime.now():%H:%M:%S}"
        f"</div>",
        unsafe_allow_html=True,
    )


def render_kpis(view: dict) -> None:
    """Single row of clean KPI cards. Numbers only — no captions."""
    chg_pct = view["change_pct"]
    chg_abs = view["change_abs"]
    chg_unit = view["change_per_unit_abs"]
    df = view["holdings_df"]
    cols = st.columns(5)
    cols[0].metric(
        "Live NAV / unit (EUR)",
        f"{view['live_nav_per_unit']:.4f}",
        delta=f"{chg_unit:+,.4f}",
    )
    cols[1].metric(
        "Daily NAV change",
        f"{chg_pct:+.2f}%",
        delta=f"{chg_abs:+,.0f} EUR",
    )
    cols[2].metric("Live NAV (EUR)", f"{view['live_nav']:,.0f}")
    cols[3].metric("Holdings", f"{len(df)}")
    cols[4].metric(
        "Stale (no live)",
        f"{view['stale_count']}",
        help="Holdings without a live quote — held flat at book value.",
    )


def render_holdings_table(df: pd.DataFrame, key_prefix: str = "") -> None:
    if df.empty:
        st.info("No holdings.")
        return

    with st.container():
        c1, c2, c3 = st.columns([3, 2, 2])
        search = c1.text_input(
            "Search", placeholder="Filter by security / ticker / ISIN…",
            key=f"{key_prefix}search", label_visibility="collapsed",
        )
        classes = c2.multiselect(
            "Asset class", sorted(df["Asset class"].unique()),
            key=f"{key_prefix}classes", placeholder="All asset classes",
            label_visibility="collapsed",
        )
        sectors = c3.multiselect(
            "Sector", sorted(df["Sector"].unique()),
            key=f"{key_prefix}sectors", placeholder="All sectors",
            label_visibility="collapsed",
        )

    view = df.copy()
    if search:
        s = search.lower()
        mask = (view["Security"].str.lower().str.contains(s, na=False) |
                view["Ticker"].str.lower().str.contains(s, na=False) |
                view["ISIN"].str.lower().str.contains(s, na=False))
        view = view[mask]
    if classes:
        view = view[view["Asset class"].isin(classes)]
    if sectors:
        view = view[view["Sector"].isin(sectors)]

    # Drop rarely-needed columns so the table fits cleanly on the left half.
    display = view[[
        "Security", "Ticker", "Asset class", "Sector",
        "Quantity", "Price (live)", "MV (EUR)", "Weight",
        "Δ (%)", "Live",
    ]].copy()
    display["Weight"] = display["Weight"] * 100

    st.dataframe(
        display.sort_values("MV (EUR)", ascending=False),
        use_container_width=True,
        hide_index=True,
        height=520,
        column_config={
            "Security": st.column_config.TextColumn("Security", width="medium"),
            "Ticker": st.column_config.TextColumn("Ticker", width="small"),
            "Asset class": st.column_config.TextColumn("Class", width="small"),
            "Sector": st.column_config.TextColumn("Sector", width="small"),
            "Quantity": st.column_config.NumberColumn("Qty", format="%.0f"),
            "Price (live)": st.column_config.NumberColumn("Px", format="%.2f"),
            "MV (EUR)": st.column_config.NumberColumn("MV (EUR)", format="%.0f"),
            "Weight": st.column_config.NumberColumn("Wt %", format="%.2f%%"),
            "Δ (%)": st.column_config.NumberColumn("Δ %", format="%+.2f%%"),
            "Live": st.column_config.TextColumn("●", width="small"),
        },
    )

    csv_bytes = view.to_csv(index=False).encode("utf-8")
    excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine="openpyxl") as xl:
        view.to_excel(xl, sheet_name="Holdings", index=False)
    excel_buf.seek(0)

    e1, e2, _ = st.columns([1, 1, 6])
    e1.download_button(
        "⬇ CSV", data=csv_bytes,
        file_name=f"holdings_{datetime.now():%Y%m%d_%H%M}.csv",
        mime="text/csv", key=f"{key_prefix}csv", use_container_width=True,
    )
    e2.download_button(
        "⬇ Excel", data=excel_buf.getvalue(),
        file_name=f"holdings_{datetime.now():%Y%m%d_%H%M}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{key_prefix}xlsx", use_container_width=True,
    )


def _pie(df: pd.DataFrame, key: str, height: int = 520) -> go.Figure:
    agg = (df.groupby(key, as_index=False)["MV (EUR)"].sum()
             .sort_values("MV (EUR)", ascending=False))
    fig = px.pie(agg, names=key, values="MV (EUR)", hole=0.5)
    fig.update_traces(textposition="inside", textinfo="percent+label",
                      hovertemplate="<b>%{label}</b><br>EUR %{value:,.0f}<br>%{percent}")
    fig.update_layout(
        height=height,
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="v", yanchor="middle", y=0.5, x=1.02, font=dict(size=11)),
        showlegend=True,
    )
    return fig


def render_sector_pie(df: pd.DataFrame) -> None:
    if df.empty:
        return
    st.plotly_chart(_pie(df, "Sector", height=520), use_container_width=True)


def _allocation_table(df: pd.DataFrame, group_col: str, label: str) -> None:
    """Compact tabular allocation breakdown — alternative to a pie chart."""
    agg = (df.groupby(group_col, as_index=False)["MV (EUR)"].sum()
             .sort_values("MV (EUR)", ascending=False))
    total = agg["MV (EUR)"].sum() or 1
    agg["Weight %"] = (agg["MV (EUR)"] / total * 100).round(2)
    agg["MV (EUR)"] = agg["MV (EUR)"].round(0)
    agg = agg.rename(columns={group_col: label})
    st.dataframe(
        agg,
        use_container_width=True,
        hide_index=True,
        height=min(40 + len(agg) * 35, 260),
        column_config={
            label: st.column_config.TextColumn(label),
            "MV (EUR)": st.column_config.NumberColumn("MV (EUR)", format="%.0f"),
            "Weight %": st.column_config.NumberColumn("Weight %", format="%.2f%%"),
        },
    )


def render_secondary_allocations(df: pd.DataFrame) -> None:
    if df.empty:
        return
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("### Asset class")
        _allocation_table(df, "Asset class", "Asset class")
    with c2:
        st.markdown("### Currency")
        _allocation_table(df.assign(Currency=df["Currency"].replace("", "—")),
                          "Currency", "Currency")
    with c3:
        st.markdown("### Country")
        _allocation_table(df.assign(Country=df["Country"].replace("", "Unknown")),
                          "Country", "Country")


def render_top10(df: pd.DataFrame) -> None:
    if df.empty:
        return
    top = df.sort_values("MV (EUR)", ascending=True).tail(10).copy()
    fig = go.Figure(go.Bar(
        x=top["MV (EUR)"],
        y=top["Security"].str[:42],
        orientation="h",
        text=[f"{v*100:.1f}%" for v in top["Weight"]],
        textposition="outside",
        marker=dict(color="#4ea1ff", line=dict(width=0)),
        hovertemplate="<b>%{y}</b><br>EUR %{x:,.0f}<extra></extra>",
    ))
    fig.update_layout(
        height=380,
        margin=dict(l=0, r=24, t=10, b=0),
        xaxis_title="Market value (EUR)",
        yaxis_title=None,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)


def render_nav_history(funds: list, fund_label: str) -> None:
    rows = [
        {"Date": f.reporting_date, "NAV/unit": f.nav_per_unit}
        for f in funds if f.fund_name == fund_label
    ]
    if len(rows) < 2:
        st.caption("Drop more dated xlsx files into `data/` to build a NAV history chart.")
        return
    hist = pd.DataFrame(rows).sort_values("Date")
    fig = px.line(hist, x="Date", y="NAV/unit", markers=True)
    fig.update_traces(line=dict(color="#4ea1ff", width=2))
    fig.update_layout(
        height=240, margin=dict(l=0, r=0, t=10, b=0),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    sig = _data_signature(DATA_DIR)
    if not sig:
        st.warning("No .xlsx files found in `data/`. Drop your fund reports there.")
        return
    funds = load_funds(sig)
    if not funds:
        st.error("Could not parse any files in `data/`.")
        return

    labels = sorted({f.fund_name for f in funds})

    with st.sidebar:
        st.markdown("### Fund")
        choice = st.radio("Fund", labels, label_visibility="collapsed")
        st.markdown("---")
        if st.button("↻ Refresh prices now", use_container_width=True):
            pricing.clear_cache()
            st.rerun()
        st.caption("Cache TTL: 60s ok / 5 min on failure.")
        st.markdown("---")
        st.caption(f"Loaded {len(funds)} file(s):")
        for f in funds:
            st.caption(f"• {f.fund_name} — {f.reporting_date}")

    fund = next(f for f in funds if f.fund_name == choice)
    view = reprice_fund(fund)

    # 1. Title + KPIs
    render_title(fund)
    render_kpis(view)

    st.markdown("")  # spacer

    # 2. Holdings table (left) + Sector pie (right)
    left, right = st.columns([1.5, 1])
    with left:
        st.markdown("### Holdings")
        render_holdings_table(view["holdings_df"], key_prefix=fund.fund_name + "_")
    with right:
        st.markdown("### Sector allocation")
        render_sector_pie(view["holdings_df"])

    st.markdown("---")

    # 3. Secondary allocations (compact tables)
    render_secondary_allocations(view["holdings_df"])

    st.markdown("---")

    # 4. Top 10 holdings + NAV history
    c1, c2 = st.columns([1.2, 1])
    with c1:
        st.markdown("### Top 10 holdings")
        render_top10(view["holdings_df"])
    with c2:
        st.markdown("### NAV / unit history")
        render_nav_history(funds, fund.fund_name)

    # Footer
    st.markdown("---")
    st.caption(
        f"USD/EUR live: **{view['fx_used']:.4f}** (book {view['fx_book']:.4f})  ·  "
        f"Units outstanding: **{fund.units_outstanding:,.4f}**  ·  "
        f"NAV % = SUMPRODUCT(weight × daily-return) over priced holdings"
    )


if __name__ == "__main__":
    main()
