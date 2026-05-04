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
BGN_PER_EUR = 1.95583  # fixed peg (display only — values in xlsx are EUR)


# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Funds Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Auto-refresh: re-runs the script every REFRESH_SECONDS without user action.
# Uses streamlit's built-in fragment-style rerun via st.experimental_set_query_params trick.
# Cleanest cross-version approach: a small JS embed.
st.markdown(
    f"<meta http-equiv='refresh' content='{REFRESH_SECONDS}'>",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Data loading (cached on file mtimes)
# ---------------------------------------------------------------------------
def _data_signature(folder: Path) -> tuple:
    """A cache key that changes whenever any xlsx in `data/` is edited or
    when files appear/disappear."""
    return tuple(sorted(
        (p.name, p.stat().st_mtime_ns)
        for p in folder.glob("*.xlsx")
        if not p.name.startswith("~$")
    ))


@st.cache_data(show_spinner=False)
def load_funds(signature: tuple):
    """Parse every .xlsx in data/. Cache key = file mtimes, so editing /
    adding a file invalidates this automatically."""
    funds = fund_parser.parse_data_dir(DATA_DIR)
    for f in funds:
        enrich_fund(f)
    return funds


# ---------------------------------------------------------------------------
# Live pricing — re-priced on every script run (no streamlit cache here, the
# pricing module handles its own short TTL)
# ---------------------------------------------------------------------------
def reprice_fund(fund) -> dict:
    """Compute live NAV and per-holding daily change for one fund.

    Returns a dict with:
      live_nav, prev_nav, live_nav_per_unit, prev_nav_per_unit,
      change_abs, change_pct, holdings_df, fx_used, stale_count
    """
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
                # unknown currency: scale proportionally from xlsx mv
                ratio = q.last / q.prev if q.prev else 1.0
                live_mv = h.market_value * ratio
                prev_mv = h.market_value
            live_price = q.last
            prev_price = q.prev
            is_live = True
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
            "Market value (live, EUR)": live_mv,
            "Δ (EUR)": live_mv - prev_mv,
            "Δ (%)": ((live_mv / prev_mv - 1) * 100) if prev_mv else 0.0,
            "Weight": 0.0,  # filled in below
            "Live": "✓" if is_live else "—",
            "Note": note,
        })

    # Non-holdings ("other assets") = cash + receivables + deferred expenses.
    # Easiest to derive from the xlsx totals: total_assets − Σ holdings(book).
    book_holdings_total = sum(h.market_value for h in fund.holdings)
    other_assets = (fund.total_assets or (book_holdings_total + sum(c.market_value for c in fund.cash))) - book_holdings_total
    cash_total = sum(c.market_value for c in fund.cash)
    liabilities = fund.total_liabilities

    live_nav = live_holdings_total + other_assets - liabilities
    prev_nav = prev_holdings_total + other_assets - liabilities

    # Compute weights against the live total assets (holdings + other)
    live_total_assets = live_holdings_total + other_assets
    for r in rows:
        r["Weight"] = (r["Market value (live, EUR)"] / live_total_assets) if live_total_assets else 0.0

    df = pd.DataFrame(rows)

    units = fund.units_outstanding or 1
    return {
        "fund": fund,
        "live_nav": live_nav,
        "prev_nav": prev_nav,
        "live_nav_per_unit": live_nav / units,
        "prev_nav_per_unit": prev_nav / units,
        "change_abs": live_nav - prev_nav,
        "change_pct": ((live_nav / prev_nav - 1) * 100) if prev_nav else 0.0,
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
# Layout
# ---------------------------------------------------------------------------
def fmt_money(v: float, ccy: str = "BGN", decimals: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{decimals}f} {ccy}"


def fmt_pct(v: float, decimals: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:+.{decimals}f}%"


def render_kpis(view: dict) -> None:
    nav = view["live_nav"]
    chg_abs = view["change_abs"]
    chg_pct = view["change_pct"]
    nav_unit = view["live_nav_per_unit"]
    chg_unit = view["change_per_unit_abs"]
    fund = view["fund"]
    n_holdings = len(view["holdings_df"])
    n_currencies = view["holdings_df"]["Currency"].replace("", pd.NA).dropna().nunique()
    top_w = view["holdings_df"]["Weight"].max() if not view["holdings_df"].empty else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(
        "Live NAV / unit (EUR)",
        f"{nav_unit:,.4f}",
        delta=f"{chg_unit:+,.4f}  ({chg_pct:+.2f}%)",
    )
    c2.metric("Live NAV (EUR)", f"{nav:,.0f}", delta=f"{chg_abs:+,.0f}")
    c3.metric("Holdings", f"{n_holdings}")
    c4.metric("Currencies", f"{n_currencies}")
    c5.metric("Top holding wt.", f"{top_w*100:.1f}%")

    sub = st.columns(4)
    sub[0].caption(f"Reporting date: **{fund.reporting_date.isoformat()}**")
    sub[1].caption(f"Units outstanding: **{fund.units_outstanding:,.4f}**")
    sub[2].caption(f"USD→EUR live: **{view['fx_used']:.4f}**  (book {view['fx_book']:.4f})")
    sub[3].caption(f"Stale (no live): **{view['stale_count']}** of {n_holdings}")


def render_allocation_charts(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No holdings to display.")
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Asset class**")
        agg = df.groupby("Asset class", as_index=False)["Market value (live, EUR)"].sum()
        fig = px.pie(agg, names="Asset class", values="Market value (live, EUR)", hole=0.45)
        fig.update_layout(showlegend=True, height=320, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    with c2:
        st.markdown("**Currency**")
        agg = (df.assign(Currency=df["Currency"].replace("", "—"))
                 .groupby("Currency", as_index=False)["Market value (live, EUR)"].sum())
        fig = px.pie(agg, names="Currency", values="Market value (live, EUR)", hole=0.45)
        fig.update_layout(showlegend=True, height=320, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    with c3:
        st.markdown("**Country**")
        agg = (df.assign(Country=df["Country"].replace("", "Unknown"))
                 .groupby("Country", as_index=False)["Market value (live, EUR)"].sum())
        fig = px.pie(agg, names="Country", values="Market value (live, EUR)", hole=0.45)
        fig.update_layout(showlegend=True, height=320, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)


def render_sector_chart(df: pd.DataFrame) -> None:
    if df.empty:
        return
    agg = (df.groupby("Sector", as_index=False)["Market value (live, EUR)"].sum()
             .sort_values("Market value (live, EUR)", ascending=False))
    fig = px.pie(agg, names="Sector", values="Market value (live, EUR)", hole=0.4)
    fig.update_layout(height=380, margin=dict(l=0, r=0, t=20, b=0))
    st.plotly_chart(fig, use_container_width=True)


def render_top10(df: pd.DataFrame) -> None:
    if df.empty:
        return
    top = (df.sort_values("Market value (live, EUR)", ascending=True).tail(10))
    fig = go.Figure(go.Bar(
        x=top["Market value (live, EUR)"],
        y=top["Security"].str[:40],
        orientation="h",
        text=[f"{v*100:.1f}%" for v in top["Weight"]],
        textposition="outside",
        marker=dict(color="#4ea1ff"),
    ))
    fig.update_layout(
        height=380,
        margin=dict(l=0, r=20, t=10, b=0),
        xaxis_title="Market value (EUR)",
        yaxis_title=None,
    )
    st.plotly_chart(fig, use_container_width=True)


def render_holdings_table(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No holdings.")
        return

    with st.container():
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        search = c1.text_input("Search", placeholder="Filter by security / ticker / ISIN…")
        classes = c2.multiselect("Asset class", sorted(df["Asset class"].unique()))
        ccys = c3.multiselect("Currency", sorted(c for c in df["Currency"].unique() if c))
        sectors = c4.multiselect("Sector", sorted(df["Sector"].unique()))

    view = df.copy()
    if search:
        s = search.lower()
        mask = (view["Security"].str.lower().str.contains(s, na=False) |
                view["Ticker"].str.lower().str.contains(s, na=False) |
                view["ISIN"].str.lower().str.contains(s, na=False))
        view = view[mask]
    if classes:
        view = view[view["Asset class"].isin(classes)]
    if ccys:
        view = view[view["Currency"].isin(ccys)]
    if sectors:
        view = view[view["Sector"].isin(sectors)]

    display = view.copy()
    display["Weight"] = (display["Weight"] * 100).round(2)
    display["Δ (%)"] = display["Δ (%)"].round(2)
    display["Market value (live, EUR)"] = display["Market value (live, EUR)"].round(2)
    display["Δ (EUR)"] = display["Δ (EUR)"].round(2)
    display["Quantity"] = display["Quantity"].round(2)
    display["Price (live)"] = display["Price (live)"].round(4)
    display["Price (book)"] = display["Price (book)"].round(4)

    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        height=460,
        column_config={
            "Weight": st.column_config.NumberColumn("Weight %", format="%.2f%%"),
            "Δ (%)": st.column_config.NumberColumn("Δ %", format="%+.2f%%"),
            "Market value (live, EUR)": st.column_config.NumberColumn("MV (EUR)", format="%.2f"),
            "Δ (EUR)": st.column_config.NumberColumn("Δ EUR", format="%+.2f"),
        },
    )

    # --- Export buttons
    csv_bytes = view.to_csv(index=False).encode("utf-8")
    excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine="openpyxl") as xl:
        view.to_excel(xl, sheet_name="Holdings", index=False)
    excel_buf.seek(0)

    e1, e2, _ = st.columns([1, 1, 6])
    e1.download_button(
        "⬇ CSV",
        data=csv_bytes,
        file_name=f"holdings_{datetime.now():%Y%m%d_%H%M}.csv",
        mime="text/csv",
        use_container_width=True,
    )
    e2.download_button(
        "⬇ Excel",
        data=excel_buf.getvalue(),
        file_name=f"holdings_{datetime.now():%Y%m%d_%H%M}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )


def render_nav_history(funds: list, fund_label: str | None) -> None:
    """If multiple xlsx files exist for the same fund (different reporting
    dates), draw NAV/unit over time."""
    rows = []
    for f in funds:
        if fund_label and f.fund_name != fund_label:
            continue
        rows.append({
            "Date": f.reporting_date,
            "Fund": f.fund_name,
            "NAV/unit": f.nav_per_unit,
        })
    if len(rows) < 2:
        st.caption("Drop more dated xlsx files into `data/` to build a NAV history chart.")
        return
    hist = pd.DataFrame(rows)
    fig = px.line(hist, x="Date", y="NAV/unit", color="Fund", markers=True)
    fig.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
def main() -> None:
    st.title("📊 Funds Dashboard")
    st.caption(
        f"Live re-pricing via yfinance · Auto-refresh every {REFRESH_SECONDS}s · "
        f"Last tick: {datetime.now():%H:%M:%S}"
    )

    sig = _data_signature(DATA_DIR)
    if not sig:
        st.warning("No .xlsx files found in `data/`. Drop your fund reports there.")
        return
    funds = load_funds(sig)
    if not funds:
        st.error("Could not parse any files in `data/`.")
        return

    # Sidebar
    with st.sidebar:
        st.header("View")
        labels = sorted({f.fund_name for f in funds})
        choice = st.selectbox(
            "Fund",
            ["Combined"] + labels,
            help="Combined merges all funds' holdings into a single view.",
        )
        st.markdown("---")
        if st.button("↻ Refresh prices now", use_container_width=True):
            pricing.clear_cache()
            st.rerun()
        st.caption("Cache TTL: 60s for successful quotes, 5 min for failed ones.")
        st.markdown("---")
        st.caption(f"Loaded {len(funds)} fund file(s):")
        for f in funds:
            st.caption(f"• {f.fund_name} — {f.reporting_date} ({len(f.holdings)} hldg)")

    # Pick the selected funds
    selected = funds if choice == "Combined" else [f for f in funds if f.fund_name == choice]

    # Reprice each
    views = [reprice_fund(f) for f in selected]

    # Combine into a synthetic "view" if needed
    if len(views) == 1:
        view = views[0]
        single_label = view["fund"].fund_name
    else:
        view = _combine_views(views, label="Combined")
        single_label = None

    # KPIs
    render_kpis(view)
    st.markdown("---")

    # Allocation row
    render_allocation_charts(view["holdings_df"])

    # Top 10 + sector
    st.markdown("---")
    c1, c2 = st.columns([1, 1])
    with c1:
        st.markdown("### Top 10 holdings")
        render_top10(view["holdings_df"])
    with c2:
        st.markdown("### Sector allocation")
        render_sector_chart(view["holdings_df"])

    # NAV history
    st.markdown("---")
    st.markdown("### NAV / unit history")
    render_nav_history(funds, single_label)

    # Holdings table
    st.markdown("---")
    st.markdown("### Holdings")
    render_holdings_table(view["holdings_df"])

    # NAV source config
    st.markdown("---")
    with st.expander("⚙ NAV source config"):
        st.markdown(
            "Currently NAV is computed real-time from holdings × live prices. "
            "If you want to layer a published NAV feed on top, edit `config.yaml::nav_sources` "
            "with a `url` per fund (JSON or HTML), then restart the app. "
            "Historical NAV is built up automatically as you drop more dated xlsx files into `data/`."
        )


def _combine_views(views: list[dict], label: str) -> dict:
    """Merge per-fund views into a single composite view for 'Combined' mode."""
    df = pd.concat([v["holdings_df"] for v in views], ignore_index=True)
    live_nav = sum(v["live_nav"] for v in views)
    prev_nav = sum(v["prev_nav"] for v in views)
    units = sum(v["fund"].units_outstanding for v in views)
    cash_total = sum(v["cash_total"] for v in views)
    stale = sum(v["stale_count"] for v in views)
    other = sum(v["other_assets"] for v in views)
    liab = sum(v["liabilities"] for v in views)
    live_holdings = sum(v["live_holdings_total"] for v in views)

    # Weight against combined total assets
    total_assets = live_holdings + other
    if total_assets:
        df["Weight"] = df["Market value (live, EUR)"] / total_assets

    class _Stub:
        fund_name = label
        reporting_date = max(v["fund"].reporting_date for v in views)
        units_outstanding = units

    return {
        "fund": _Stub(),
        "live_nav": live_nav,
        "prev_nav": prev_nav,
        "live_nav_per_unit": (live_nav / units) if units else 0,
        "prev_nav_per_unit": (prev_nav / units) if units else 0,
        "change_abs": live_nav - prev_nav,
        "change_pct": ((live_nav / prev_nav - 1) * 100) if prev_nav else 0,
        "change_per_unit_abs": ((live_nav - prev_nav) / units) if units else 0,
        "holdings_df": df,
        "live_holdings_total": live_holdings,
        "other_assets": other,
        "liabilities": liab,
        "cash_total": cash_total,
        "fx_used": views[0]["fx_used"],
        "fx_book": views[0]["fx_book"],
        "eurusd": views[0]["eurusd"],
        "stale_count": stale,
    }


if __name__ == "__main__":
    main()
