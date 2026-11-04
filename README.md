# Earnings Desk

A static site that tracks quarterly results for AAPL, NVDA, MSFT, GOOGL, AMZN, AVGO, TSLA, META, WMT, LLY and AMD, with data pulled straight from SEC filings.

```
SEC EDGAR API ──► fetch_data.py ──► data.json ─┐
                  (GitHub Actions,             ├──► index.html (GitHub Pages)
                   weekday mornings)           │
                          notes.json (by hand) ┘
```

| File | Role |
|---|---|
| `fetch_data.py` | Pulls XBRL facts from SEC's companyfacts API, normalizes tags, derives Q4, writes `data.json` |
| `data.json` | Generated. Last 8 fiscal quarters per company. Don't edit by hand |
| `notes.json` | Hand-maintained beat/miss and guidance notes, keyed by ticker and fiscal period |
| `index.html` | Front end. Fetches both JSON files and renders the tables, chart and filing links; pulls live prices from Finnhub in the browser. No build step, no dependencies |
| `config.example.js` | Template for `config.js`, which holds the Finnhub key. `config.js` is gitignored; the deploy job generates it from the `FINNHUB_API_KEY` secret |
| `.github/workflows/update-data.yml` | Runs the fetcher Mon–Fri, commits `data.json` when it changes, then deploys the site to GitHub Pages (also redeploys on every push to main) |

## Run locally

```bash
pip install -r requirements.txt
python3 fetch_data.py
cp config.example.js config.js  # paste your Finnhub key in (optional; prices are off without it)
python3 -m http.server 8000     # then open http://localhost:8000
```

(Opening `index.html` straight from disk won't work: browsers block `fetch()` on `file://` URLs.)

## Data notes

- All figures are GAAP, as filed. Values marked ◆ on the page are derived:
  - **Q4** = 10-K annual figure − (Q1 + Q2 + Q3). For EPS, Q4 net income is divided by derived Q4 diluted shares, because per-share values don't subtract cleanly.
  - **Gross profit** = revenue − cost of revenue, for companies that don't report a gross profit line (AMZN, WMT, META, GOOGL, LLY).
  - **LLY operating income** = pre-tax income − non-operating income, because Lilly's income statement has no operating income subtotal.
- Fiscal periods use each company's own calendar (NVDA's FY2027 Q2 ended July 2026).
- Filing links come from SEC's submissions API (`data.sec.gov/submissions/`): the last 5 10-Ks and 8 10-Qs per company, plus any amendments, linking straight to sec.gov.
- Share prices come from Finnhub's `/quote` endpoint, called from the browser every 60 seconds while the tab is visible. P/E = price ÷ sum of the last four quarters of diluted EPS.
