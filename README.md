# Earnings Desk

Quarterly results, EPS beat/miss, analyst ratings, price targets and prices for every S&P 500 and Nasdaq-100 company (~518 tickers), built from SEC filings and published as a static site on GitHub Pages.

```
                       ┌─ daily, 6:30am ET ─────────────────────────────┐
Wikipedia (index lists) ─► universe.py ─┐                                 │
SEC bulk XBRL zips ──────► sec.py ──────┼─► fetch_data.py ─► data/companies/<T>.json
Finnhub (EPS, ratings) ─► finnhub.py ─┤
FMP / Alpha Vantage ────► targets.py ─┘                 └─► data/index.json ◄─┐
                       └────────────────────────────────────────────────┘     │
Finnhub quotes ─► update_prices.py (every 30 min, market hours) ──────────────┘
                                                                   │
                                   index.html (GitHub Pages) ◄─────┘
```

| File | Role |
|---|---|
| `universe.py` | Which companies: S&P 500 + Nasdaq-100 from Wikipedia's constituent tables, or every US-listed stock (`UNIVERSE=all`) |
| `sec.py` | SEC data: bulk-zip or per-company API source, XBRL tag mapping, Q4 derivation, fiscal calendars, filing links |
| `finnhub.py` | Rate-limited Finnhub client (50 calls/min, with retries and backoff, under the 60/min free tier) |
| `targets.py` | Price targets: tests Finnhub → FMP → Alpha Vantage each day and uses the first that works; rotates under the source's daily cap |
| `fetch_data.py` | Daily pipeline: fundamentals, filings, EPS surprises, logos, analyst ratings, price targets → data files |
| `update_prices.py` | Price refresh into `data/index.json`, every 30 min in US market hours |
| `store.py` | Data file layout and writers |
| `index.html` | The site: ticker tape, search, featured cards, sortable table, company detail. No build step |
| `notes.json` | Optional hand-written notes/guidance; a `result` here overrides the automatic beat/miss |

## Data files
- `data/index.json`: one row per company (identity, logo, latest quarter, latest EPS surprise, price, market cap). Loaded on page open, re-checked every 5 minutes.
- `data/companies/<TICKER>.json`: 8 quarters, filing links, last 4 EPS surprises, 4 months of analyst rating counts, price target with source and as-of date. Loaded when you open a company.
- `index.json` → `sources.price_targets` records which target source is in use and what each source returned on the latest test.

## API keys stay secret
Every API call runs inside GitHub Actions with keys from repo secrets (`FINNHUB_API_KEY`, `FMP_API_KEY`, optional `ALPHAVANTAGE_API_KEY`). The site only reads JSON files, so no key reaches a browser.

## Workflows
- `update-data.yml`: weekdays 11:30 UTC. Downloads SEC's bulk zips (~3 GB, read selectively), calls Finnhub (~19 min first run, ~10 min after), commits `data/`, deploys.
- `update-prices.yml`: every 30 min, 13:00–21:30 UTC weekdays; exits early outside 9:30am–4:45pm ET and on holidays. ~520 quotes ≈ 10 min. Finnhub's free quotes don't include pre-market or after-hours trades (outside the session they return the 4pm close), so extended hours aren't scheduled; the site labels those prices "At close".
- `deploy.yml`: publishes the site. Called by both jobs, and runs on your own pushes.
Both data jobs share a concurrency group, so they never run at once (no doubled Finnhub rate, no push races).

## Run locally
```bash
pip install -r requirements.txt
python3 fetch_data.py --source api --only AAPL,NVDA   # quick: per-company SEC API, two tickers
python3 fetch_data.py                                 # full: downloads SEC bulk zips into .cache/
FINNHUB_API_KEY=... python3 update_prices.py --force  # prices outside market hours
python3 -m http.server 8000                           # open http://localhost:8000
```

## Switching to all US-listed stocks
Set `UNIVERSE: all` in both workflows' `env` (or export it locally). `universe.py` then uses SEC's `company_tickers_exchange.json` (Nasdaq, NYSE, CBOE: ~7,700 tickers). SEC processing still takes about a minute thanks to the bulk zips. Finnhub is the constraint: at 60 calls/min, prices for 7,700 tickers take ~2.4 hours, so each 30-minute run updates the 1,400 stalest prices and the daily job rotates EPS updates oldest-first. Full coverage at the same cadence needs a paid Finnhub plan.

## Analyst ratings and price targets
- **Ratings**: Finnhub `/stock/recommendation` (free), refreshed daily for every company. Consensus = average on a 1 (Strong Buy) to 5 (Strong Sell) scale; ≤1.5 Strong Buy, ≤2.5 Buy, ≤3.5 Hold, otherwise Sell.
- **Price targets**: two free sources, split by ticker (Finnhub's own endpoint is premium-only; it is re-tested once a day in case the plan changes):
  1. Financial Modeling Prep `price-target-consensus` (`FMP_API_KEY`) for the tickers its free plan covers (a sample of popular names). Mean, median, high and low. Free plan: 250 calls/day; the job uses at most 240. Tickers FMP doesn't cover are remembered and re-checked monthly, so daily calls go to covered tickers.
  2. Alpha Vantage `OVERVIEW` → `AnalystTargetPrice` (`ALPHAVANTAGE_API_KEY`) for everything else, oldest first. Free plan: 25 calls/day; the job uses at most 24, so a full pass over the uncovered tickers takes about 20 trading days. Mean target only; the site says "Mean target only".
  - Calls per source per day are counted in `index.json` (`sources.price_targets.usage`), so a second manual run on the same day can't go over either limit.
  - `store.py` refuses to write any data file that contains an API key value.
- Each target stores its source and as-of date; upside = mean target ÷ latest price − 1, computed in the browser so it moves with the 30-minute prices.

## Data notes
- GAAP figures as filed. ◆ marks a derived value: Q4 = annual − (Q1+Q2+Q3); Q4 EPS = derived Q4 net income ÷ derived Q4 diluted shares; gross profit = revenue − cost of revenue when not reported; bank revenue = net interest income + noninterest income.
- Beat/miss uses Finnhub's EPS actual vs. consensus estimate, which is usually *adjusted* EPS, so it can differ from the GAAP EPS shown beside it. "In line" means equal to the cent.
- Foreign filers that report under IFRS (ASML, ARM, PDD and a few others; 20-F/40-F) show prices and filing links but no quarterly figures.
