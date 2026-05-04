# Funds Dashboard

A real-time NAV dashboard for mutual-fund holdings reports. Drop daily `.xlsx`
files into `data/`, the app re-prices every holding live via Yahoo Finance and
recomputes NAV/unit on a 60-second loop.

Built for the Bulgarian "Thracian-Invest" fund-report template, but the parser
is column-aware (matches by header text, not fixed positions) so other funds
that follow a similar nested layout should work too.

## Features

- **Fund selector** — switch between each fund or a Combined view
- **Live NAV/unit** — Σ(qty × live price × FX) + cash − liabilities, recomputed every 60s
- **Daily change** — today's live values vs yesterday's close (per-holding and aggregated)
- **Allocation pies** — by asset class, currency, country
- **Top-10 holdings** bar chart
- **Sector pie** — driven by editable mapping in `config.yaml`
- **Holdings table** — search, multi-column filters, sort, CSV / Excel export
- **NAV history** — automatic, grows as you drop more dated `.xlsx` files into `data/`
- **Stale handling** — holdings yfinance can't price (BG bonds, options) are held flat at book value with a "—" badge
- **Dark theme** — configured in `.streamlit/config.toml`; switch to light by editing `base = "light"`

## Quick start (local)

```bash
pip install -r requirements.txt
streamlit run app.py
```

Open http://localhost:8501.

The `data/` folder is pre-populated with the two report files; you can drop more
in at any time and the dashboard picks them up on the next refresh tick.

## Project layout

```
Funds/
├── app.py                 # Streamlit UI + reprice engine
├── parser.py              # XLSX → Fund objects
├── enrichment.py          # ISIN→country, ticker→sector
├── pricing.py             # yfinance live prices + caching
├── config.yaml            # sector overrides, ticker overrides, NAV-source URLs
├── requirements.txt
├── data/                  # drop your fund .xlsx files here
└── .streamlit/config.toml # theme + server settings
```

## How NAV is computed

For each fund:

```
live_NAV  =  Σ holdings(live)  +  other_assets(book)  −  liabilities(book)

  holdings(live)   = qty × live_price × FX-to-EUR        # if yfinance has it
                  = book_market_value                    # otherwise (held flat)
  other_assets    = (xlsx total assets) − Σ book_market_values_of_holdings
                  = cash + receivables + deferred-expenses
  liabilities     = parsed from "Общо текущи пасиви" line
```

Daily change uses the same formula with previous-session closes instead of live
prices. So **Δ%** is "today vs yesterday" — not "today vs the xlsx reporting
date". The cumulative gain since the reporting date is implicit in the gap
between book NAV/unit (from the file footer) and live NAV/unit (in the KPI panel).

### FX

The xlsx values are in **EUR**. Live USD→EUR is fetched once per refresh
from yfinance (`EURUSD=X`); the xlsx-stored rate is used as a fallback.
BGN is referenced only for display (it's pegged to EUR at 1.95583 by law).

## Deployment

### Streamlit Community Cloud (recommended, free)

1. Push this folder to a public GitHub repo.
2. Go to https://share.streamlit.io, click **New app**, connect your repo.
3. Main file: `app.py`. No secrets needed (yfinance has no API key).
4. Click **Deploy**. Done.

To update: drop new `.xlsx` files into the `data/` folder and `git push` —
Streamlit Cloud redeploys automatically.

### Render

1. Create a new **Web Service** pointing at your repo.
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:** `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0`
4. Set **Python Version** = 3.11.

### Local Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0"]
```

```bash
docker build -t funds-dashboard .
docker run -p 8501:8501 -v $(pwd)/data:/app/data funds-dashboard
```

## Configuration

`config.yaml` controls three things:

```yaml
sector_overrides:           # ticker → sector, edit to add coverage
  NVDA: Technology
ticker_overrides:           # xlsx ticker → Yahoo symbol when they differ
  BRK.A: BRK-A
nav_sources: {}             # optional: external NAV feed per fund (placeholder)
```

The built-in sector mapping in `enrichment.py` covers all tickers in the two
sample files. Add new tickers via `sector_overrides:` (no code changes needed).

## Assumptions and limitations

1. **Base currency is EUR.** Confirmed from the xlsx's "Чиста цена в евро"
   column — the value column equals `qty × USD_price × USD/EUR`. If your fund
   reports in BGN instead, edit `parser.Fund.base_currency` and adjust
   `app.reprice_fund` (the conversion is a single multiply by 1.95583).
2. **Cash, receivables, and liabilities are held flat at book values** during
   live re-pricing. They typically don't move intraday for these fund types.
3. **Stale holdings** (BG corporate bonds `ELGB`/`TBIE`, options like
   `QQQ260821P540`, money-market lines like `BIL` with zero quantity) carry
   their xlsx market value into the live NAV total but contribute zero to the
   day-on-day change. They show with a `—` Live badge.
4. **NAV history** is built from whatever dated `.xlsx` files you drop into
   `data/` — the chart needs at least two files to draw a line.
5. **Sector data** isn't in the xlsx. The shipped mapping is editable; any
   ticker not in it appears as "Unknown" with weight tracked separately so
   you can see how much you need to classify.
6. **yfinance rate limiting.** Successful quotes are cached for 60s; failures
   for 5 min. The first load of a brand-new ticker can take a moment.

## Troubleshooting

**"yfinance not installed"** — `pip install yfinance` (or `pip install -r requirements.txt`).

**A holding I expect to be live shows "—"** — yfinance probably uses a
different symbol. Add a mapping in `config.yaml::ticker_overrides` (e.g.
`BRK.A: BRK-A`).

**Sector says "Unknown"** — add the ticker to `config.yaml::sector_overrides`.

**Cyrillic shows as boxes in the table** — the table renders client-side; the
font set in `.streamlit/config.toml` falls back to system sans-serif which
supports Cyrillic on all major platforms.

**Wrong NAV/unit on first load** — confirm the xlsx is the unmodified report
template and that the `REPORT` sheet's footer rows ("Нетна стойност на
активите на един дял", "Общ брой дялове в обращение") aren't blank.

## Adding new funds

1. Drop the new `.xlsx` into `data/`.
2. Refresh the dashboard.

If the parser can't read it, the sidebar shows a parse error. The most common
issue is a renamed column header — edit `parser._find_header_row` to recognise
the new wording (or add an alias in `_resolve`'s call sites).
