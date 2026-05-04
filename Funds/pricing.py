"""
Live intraday pricing via yfinance.

Strategy
--------
For each holding ticker we want two numbers:
  - last:  most recent live trade
  - prev:  previous-session close

We fetch a 5-day daily history per ticker, take the last two daily closes as
(prev, last). On a quiet weekend we'll see two equal values; that's fine — the
daily change just reads zero.

USD positions are converted to BGN via live EUR/USD * 1.95583 (BGN is pegged
to EUR at this fixed rate by Bulgarian law).

Caching
-------
- streamlit-friendly TTL cache (60s default)
- failed lookups are remembered for 5 minutes so we don't retry on every
  refresh tick
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

try:
    import yfinance as yf
except ImportError:  # noqa: BLE001
    yf = None


# Tickers we know yfinance won't resolve — skip the round-trip.
SKIP_TICKERS = {
    # Bulgarian corporate bonds
    "ELGB", "TBIE",
    # Custom option / structured-product tickers from the xlsx
    "SL23", "BIL",
}

# Some xlsx tickers use formats Yahoo doesn't accept verbatim. Map them here.
# Examples: 'BRK.A' on Yahoo is 'BRK-A'. Preferred shares often use '-Px' on
# Yahoo, which matches the xlsx convention here, so most pass straight through.
TICKER_OVERRIDES: dict[str, str] = {
    # add overrides as discovered, e.g. "FOO.BAR": "FOO-BAR"
}

_PRICE_CACHE: dict[str, tuple[float, "PriceQuote"]] = {}
_CACHE_TTL_OK = 60.0
_CACHE_TTL_FAIL = 300.0


@dataclass
class PriceQuote:
    ticker: str
    last: float | None
    prev: float | None
    currency: str | None
    ok: bool
    error: str | None = None


def _yf_ticker(ticker: str) -> str:
    return TICKER_OVERRIDES.get(ticker, ticker)


def _fetch_one(ticker: str) -> PriceQuote:
    if yf is None:
        return PriceQuote(ticker, None, None, None, False, "yfinance not installed")
    if ticker in SKIP_TICKERS:
        return PriceQuote(ticker, None, None, None, False, "no live data")
    sym = _yf_ticker(ticker)
    try:
        # period="5d" returns up to 5 daily bars; we want the last two closes.
        hist = yf.Ticker(sym).history(period="5d", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return PriceQuote(ticker, None, None, None, False, "empty history")
        closes = [float(x) for x in hist["Close"].tolist() if x == x]  # NaN guard
        if len(closes) == 0:
            return PriceQuote(ticker, None, None, None, False, "no closes")
        last = closes[-1]
        prev = closes[-2] if len(closes) >= 2 else last
        # currency from .info if cheap; default None (assume USD for US tickers)
        return PriceQuote(ticker, last, prev, None, True)
    except Exception as exc:  # noqa: BLE001
        return PriceQuote(ticker, None, None, None, False, str(exc)[:80])


def get_quote(ticker: str) -> PriceQuote:
    """Cached, single-ticker quote. Returns a PriceQuote with .ok=False on failure."""
    if not ticker:
        return PriceQuote("", None, None, None, False, "no ticker")
    now = time.time()
    cached = _PRICE_CACHE.get(ticker)
    if cached:
        ts, q = cached
        ttl = _CACHE_TTL_OK if q.ok else _CACHE_TTL_FAIL
        if now - ts < ttl:
            return q
    q = _fetch_one(ticker)
    _PRICE_CACHE[ticker] = (now, q)
    return q


def get_quotes(tickers: Iterable[str]) -> dict[str, PriceQuote]:
    """Fetch many tickers, using cache where possible."""
    return {t: get_quote(t) for t in tickers}


def get_eurusd() -> float | None:
    """Live EUR/USD (USD per 1 EUR, e.g. 1.08).
    Returns None on failure; caller falls back to the xlsx rate."""
    q = get_quote("EURUSD=X")
    return q.last if q.ok else None


def usd_to_eur(eurusd: float | None, fallback_usd_eur: float) -> float:
    """USD → EUR.  eurusd is USD per 1 EUR (e.g. 1.08).
    fallback_usd_eur is the EUR-per-1-USD rate stored in the xlsx (e.g. 0.8546)."""
    if eurusd and eurusd > 0:
        return 1.0 / eurusd
    return fallback_usd_eur


def clear_cache() -> None:
    _PRICE_CACHE.clear()
