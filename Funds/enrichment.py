"""
Sector and country enrichment.

The xlsx files don't carry sector data. This module ships a curated mapping
covering the tickers currently in `data/`, plus a pluggable override file
(`config.yaml::sector_overrides`) so the user can extend without touching code.
"""
from __future__ import annotations

from pathlib import Path

import yaml


# Built-in sector mapping. Tickers as they appear in the xlsx 'Борсов код' column.
# Free to edit / extend via config.yaml `sector_overrides:`.
DEFAULT_SECTORS: dict[str, str] = {
    # ---- Innovation & Technology fund ----
    "QQQ": "ETF — Broad Tech",
    "SPY": "ETF — Broad Equity",
    "XLK": "ETF — Technology",
    "MAGS": "ETF — Mega-cap Tech",
    "IBIT": "ETF — Crypto",
    "IGV": "ETF — Software",
    "SMH": "ETF — Semiconductors",
    "TSLA": "Consumer Discretionary",
    "MSFT": "Technology",
    "CRCL": "Financials",
    "OKLO": "Utilities",
    "IREN": "Technology",
    "PL": "Technology",
    "ASTS": "Communication Services",
    "RKLB": "Industrials",
    "GEV": "Industrials",
    "CRWV": "Technology",
    "NVDA": "Technology",
    "INTC": "Technology",
    "ORCL": "Technology",
    "AMAT": "Technology",
    "AMD": "Technology",
    "AMZN": "Consumer Discretionary",
    "MU": "Technology",
    "GOOGL": "Communication Services",
    "META": "Communication Services",
    "SMR": "Utilities",
    "AVGO": "Technology",
    "AAPL": "Technology",
    "SOFI": "Financials",
    "RBLX": "Communication Services",
    "NFLX": "Communication Services",
    "NET": "Technology",
    "PLTR": "Technology",

    # ---- Alternative Income fund ----
    "PDI": "Closed-end Fund",
    "PTY": "Closed-end Fund",
    "FSCO": "Closed-end Fund",
    "NLY-PG": "Financials — REIT",
    "NLY-PF": "Financials — REIT",
    "ATH-PB": "Financials — Insurance",
    "ATH-PD": "Financials — Insurance",
    "TDS-PU": "Communication Services",
    "UZE": "Communication Services",
    "UZF": "Communication Services",
    "TMUSZ": "Communication Services",
    "TMUSI": "Communication Services",
    "MGRB": "Financials",
    "MGRD": "Financials",
    "COF-PJ": "Financials",
    "COF-PL": "Financials",
    "DTB": "Utilities",
    "DTG": "Utilities",
    "KIM-PM": "Real Estate",
    "WRB": "Financials — Insurance",
    "WRB-PF": "Financials — Insurance",
    "VNO-PM": "Real Estate",
    "BNJ": "Financials",
    "PBI-PB": "Industrials",
    "CTBB": "Communication Services",
    "CIM-PB": "Financials — REIT",
    "CIM-PD": "Financials — REIT",
    "ALL-PI": "Financials — Insurance",
    "JPM-PK": "Financials",
    "JPM-PL": "Financials",
    "AXS-PE": "Financials — Insurance",
    "PSA-PJ": "Real Estate",
    "SOJE": "Utilities",
    "GLOP-PB": "Energy",

    # ---- BG bonds ----
    "ELGB": "Corporate Bond — BG",
    "TBIE": "Corporate Bond — BG",
}


def _load_overrides(config_path: str | Path) -> dict[str, str]:
    p = Path(config_path)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("sector_overrides", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def lookup_sector(ticker: str | None, isin: str | None,
                  config_path: str | Path = "config.yaml") -> str:
    """Resolve a sector label, preferring user overrides over the built-in map.
    Falls back to 'Unknown' for anything unmapped."""
    overrides = _load_overrides(config_path)
    if ticker and ticker in overrides:
        return overrides[ticker]
    if isin and isin in overrides:
        return overrides[isin]
    if ticker and ticker in DEFAULT_SECTORS:
        return DEFAULT_SECTORS[ticker]
    return "Unknown"


def enrich_fund(fund) -> None:
    """Mutate fund.holdings in-place with sector labels."""
    for h in fund.holdings:
        if h.sector is None or h.sector == "Unknown":
            h.sector = lookup_sector(h.ticker, h.isin)
