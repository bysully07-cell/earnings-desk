#!/usr/bin/env python3
"""
fetch_data.py: pull the last 8 fiscal quarters of revenue, gross profit,
operating income and diluted EPS for each tracked company from SEC EDGAR,
and write them to data.json for the Earnings Desk front end.

Data source: SEC's free XBRL APIs.
  1. https://www.sec.gov/files/company_tickers.json   ticker -> CIK
  2. https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
     every XBRL fact the company has ever filed, grouped by tag and unit.

Usage:
  python3 fetch_data.py            # writes data.json next to this file
  SEC_USER_AGENT="Name email" python3 fetch_data.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TICKERS = ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "AVGO",
           "TSLA", "META", "WMT", "LLY", "AMD"]
NUM_QUARTERS = 8
NUM_10K = 5   # filing links kept per company
NUM_10Q = 8

# SEC asks every automated client to identify itself with a name and email:
# https://www.sec.gov/os/accessing-edgar-data
USER_AGENT = os.environ.get("SEC_USER_AGENT", "Benton Sullivan bysully07@gmail.com")

# SEC's fair-access limit is 10 requests/second. We stay well under it.
MIN_SECONDS_BETWEEN_REQUESTS = 0.2   # ~5 requests/second
MAX_RETRIES = 4

OUT_PATH = Path(__file__).resolve().parent / "data.json"

# Companies tag the same concept differently. Each list is in priority order;
# pick_tag() also skips tags a company stopped using years ago.
REVENUE_TAGS = [
    "Revenues",  # total revenue incl. non-contract items (TSLA leasing, WMT membership)
    "RevenueFromContractWithCustomerExcludingAssessedTax",  # ASC 606 standard tag
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",  # pre-2018 tag
]
COST_TAGS = ["CostOfRevenue", "CostOfGoodsAndServicesSold"]
GROSS_PROFIT_TAGS = ["GrossProfit"]
OPERATING_INCOME_TAGS = ["OperatingIncomeLoss"]
PRETAX_TAGS = [
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
]
NONOPERATING_TAGS = ["NonoperatingIncomeExpense"]
EPS_TAGS = ["EarningsPerShareDiluted"]

# A "quarter" is a ~13-week duration and a "year" is a ~52/53-week duration.
# Ranges allow for 14-week quarters and 53-week years (AAPL, AVGO, AMD, WMT...).
QUARTER_DAYS = range(80, 106)
ANNUAL_DAYS = range(350, 380)

# ---------------------------------------------------------------------------
# HTTP: polite, rate-limited, retrying
# ---------------------------------------------------------------------------

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
_last_request = 0.0


def get_json(url: str) -> dict:
    global _last_request
    for attempt in range(MAX_RETRIES):
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        resp = session.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            backoff = 2 ** attempt
            print(f"  {resp.status_code} from SEC, retrying in {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Gave up on {url} after {MAX_RETRIES} attempts")


# ---------------------------------------------------------------------------
# XBRL fact extraction
# ---------------------------------------------------------------------------

def d(s: str) -> date:
    return date.fromisoformat(s)


def durations(facts: dict, tag: str, unit: str) -> dict[tuple[date, date], float]:
    """
    All duration facts for one tag/unit from 10-Q and 10-K filings, keyed by
    (start, end). The same period shows up in many filings (e.g. last year's
    Q2 is repeated as a comparative in this year's Q2 10-Q); keep the most
    recently filed value so restatements win.
    """
    rows = facts.get("us-gaap", {}).get(tag, {}).get("units", {}).get(unit, [])
    best: dict[tuple[date, date], tuple[str, float]] = {}
    for r in rows:
        if "start" not in r or not r.get("form", "").startswith(("10-Q", "10-K")):
            continue
        key = (d(r["start"]), d(r["end"]))
        if key not in best or r["filed"] > best[key][0]:
            best[key] = (r["filed"], r["val"])
    return {k: v for k, (_, v) in best.items()}


def latest_end(facts: dict, tag: str, unit: str) -> date | None:
    rows = durations(facts, tag, unit)
    return max((end for _, end in rows), default=None)


def pick_tag(facts: dict, tags: list[str], unit: str) -> str | None:
    """
    Choose the tag with the most recent data; ties go to list priority.
    This handles companies that switched tags (e.g. SalesRevenueNet ->
    RevenueFromContractWithCustomer... when ASC 606 arrived in 2018).
    """
    candidates = [(latest_end(facts, t, unit), -i, t) for i, t in enumerate(tags)]
    candidates = [c for c in candidates if c[0] is not None]
    return max(candidates)[2] if candidates else None


def quarterly_series(facts: dict, tags: list[str], unit: str):
    """
    Return ({quarter_end: (quarter_start, value)}, derived_q4_ends, tag_used).

    10-Qs report 3-month figures for Q1-Q3. The 10-K usually reports only the
    full fiscal year, so Q4 = annual - (Q1 + Q2 + Q3).
    """
    tag = pick_tag(facts, tags, unit)
    if tag is None:
        return {}, set(), None

    # Primary tag first; other tags only fill gaps in periods the primary lacks.
    merged: dict[tuple[date, date], float] = {}
    for t in [tag] + [t for t in tags if t != tag]:
        for k, v in durations(facts, t, unit).items():
            merged.setdefault(k, v)

    quarters = {end: (start, v) for (start, end), v in merged.items()
                if (end - start).days in QUARTER_DAYS}
    annuals = {(start, end): v for (start, end), v in merged.items()
               if (end - start).days in ANNUAL_DAYS}

    derived = set()
    for (a_start, a_end), a_val in annuals.items():
        if a_end in quarters:  # company tagged Q4 directly; trust it
            continue
        inside = sorted((e, s, v) for e, (s, v) in quarters.items()
                        if s >= a_start - timedelta(days=7) and e < a_end)
        if len(inside) != 3:
            continue  # can't derive without exactly Q1-Q3
        q4_start = inside[-1][0] + timedelta(days=1)
        quarters[a_end] = (q4_start, a_val - sum(v for _, _, v in inside))
        derived.add(a_end)
    return quarters, derived, tag


def q4_eps_from_components(facts: dict, q4_end: date) -> float | None:
    """
    EPS is a ratio, so annual EPS - (Q1+Q2+Q3 EPS) is only approximate: the
    share count changes each quarter. More accurate:
      Q4 net income    = annual NI - (Q1+Q2+Q3 NI)
      Q4 diluted shares = 4 * annual weighted shares - (Q1+Q2+Q3 shares)
    (the annual weighted average is roughly the mean of the four quarters).
    """
    ni = durations(facts, "NetIncomeLoss", "USD")
    sh = durations(facts, "WeightedAverageNumberOfDilutedSharesOutstanding", "shares")
    for (a_start, a_end), a_ni in ni.items():
        if a_end != q4_end or (a_end - a_start).days not in ANNUAL_DAYS:
            continue
        if (a_start, a_end) not in sh:
            return None
        def inside(series):
            return sorted((e, v) for (s, e), v in series.items()
                          if (e - s).days in QUARTER_DAYS
                          and s >= a_start - timedelta(days=7) and e < a_end)
        q_ni, q_sh = inside(ni), inside(sh)
        if len(q_ni) != 3 or len(q_sh) != 3:
            return None
        q4_shares = 4 * sh[(a_start, a_end)] - sum(v for _, v in q_sh)
        if q4_shares <= 0:
            return None
        return (a_ni - sum(v for _, v in q_ni)) / q4_shares
    return None


def fiscal_label(q_end: date, annual_ends: list[date]) -> tuple[int, int]:
    """
    (fiscal_year, quarter_number) for a quarter, using the company's own
    fiscal calendar. NVDA's FY2026 ends Jan 2026, AAPL's ends Sept 2026, etc.
    Fiscal year is named after the calendar year it ends in.
    """
    prior = [e for e in annual_ends if e < q_end]
    if not prior:
        return q_end.year, 0
    fy_start = max(prior)
    q_num = max(1, min(4, round((q_end - fy_start).days / 91.3)))
    fy_end = fy_start + timedelta(days=364)
    return fy_end.year, q_num


# ---------------------------------------------------------------------------
# Per-company assembly
# ---------------------------------------------------------------------------

def build_company(ticker: str, cik: int, name: str) -> dict:
    facts = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")["facts"]

    rev, rev_d, rev_tag = quarterly_series(facts, REVENUE_TAGS, "USD")
    gp, gp_d, gp_tag = quarterly_series(facts, GROSS_PROFIT_TAGS, "USD")
    cost, cost_d, cost_tag = quarterly_series(facts, COST_TAGS, "USD")
    op, op_d, op_tag = quarterly_series(facts, OPERATING_INCOME_TAGS, "USD")
    pre, pre_d, pre_tag = quarterly_series(facts, PRETAX_TAGS, "USD")
    nonop, nonop_d, nonop_tag = quarterly_series(facts, NONOPERATING_TAGS, "USD")
    eps, eps_d, eps_tag = quarterly_series(facts, EPS_TAGS, "USD/shares")

    if not rev:
        raise RuntimeError("no revenue data found")

    # Ignore tags that went stale (e.g. AMZN's GrossProfit ends in 2009).
    newest = max(rev)
    def fresh(series):
        return bool(series) and (newest - max(series)).days < 200
    gp_method = "reported" if fresh(gp) else ("revenue - cost of revenue" if fresh(cost) else None)
    op_method = "reported" if fresh(op) else (
        "pre-tax income - non-operating income" if fresh(pre) and fresh(nonop) else None)

    annual_ends = sorted(e for (s, e) in durations(facts, rev_tag, "USD")
                         if (e - s).days in ANNUAL_DAYS)

    quarters = []
    for end in sorted(rev, reverse=True)[:NUM_QUARTERS]:
        start, revenue = rev[end]
        derived = []          # fields computed rather than read straight from a filing
        if end in rev_d:
            derived.append("revenue")

        gross = None
        if gp_method == "reported" and end in gp:
            gross = gp[end][1]
            if end in gp_d: derived.append("gross_profit")
        elif gp_method and end in cost:
            gross = revenue - cost[end][1]
            derived.append("gross_profit")

        operating = None
        if op_method == "reported" and end in op:
            operating = op[end][1]
            if end in op_d: derived.append("operating_income")
        elif op_method and end in pre and end in nonop:
            operating = pre[end][1] - nonop[end][1]
            derived.append("operating_income")

        eps_val = eps[end][1] if end in eps else None
        if end in eps_d:
            derived.append("eps_diluted")
            better = q4_eps_from_components(facts, end)
            if better is not None:
                eps_val = better

        fy, qn = fiscal_label(end, annual_ends)
        quarters.append({
            "fiscal_period": f"FY{fy} Q{qn}",
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "revenue": revenue,
            "gross_profit": gross,
            "operating_income": operating,
            "eps_diluted": round(eps_val, 2) if eps_val is not None else None,
            "derived": derived,
        })

    quarters.reverse()  # oldest -> newest, which is the natural order for charts
    return {
        "ticker": ticker,
        "name": name,
        "cik": cik,
        "fiscal_year_end": annual_ends[-1].strftime("%m-%d") if annual_ends else None,
        "sources": {
            "revenue": rev_tag,
            "gross_profit": gp_tag if gp_method == "reported" else f"derived: {gp_method} ({cost_tag})" if gp_method else None,
            "operating_income": op_tag if op_method == "reported" else f"derived: {op_method}" if op_method else None,
            "eps_diluted": eps_tag,
        },
        "quarters": quarters,
        "filings": fetch_filings(cik),
    }


def fetch_filings(cik: int) -> list[dict]:
    """
    Recent 10-K and 10-Q filings from SEC's submissions API, newest first.

    "recent" holds only a company's latest ~1,000 filings of every type.
    Big companies file lots of insider-trading Form 4s, so older 10-Ks can
    spill into overflow pages listed under filings.files; fetch those as needed.
    Amendments (10-K/A) don't count toward the quota but are kept alongside
    the original they amend.
    """
    sub = get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")["filings"]
    pages = [sub["recent"]]
    overflow = [f["name"] for f in sub.get("files", [])]

    wanted = {"10-K": NUM_10K, "10-Q": NUM_10Q}
    originals, amendments = [], []

    def scan(page):
        for i, form in enumerate(page["form"]):
            base = form.removesuffix("/A")
            if base not in wanted:
                continue
            acc = page["accessionNumber"][i]
            folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"
            row = {
                "form": form,
                "filed": page["filingDate"][i],
                "period": page["reportDate"][i],
                "url": f"{folder}/{page['primaryDocument'][i]}",
                "index_url": f"{folder}/{acc}-index.htm",
            }
            (amendments if form.endswith("/A") else originals).append(row)

    def satisfied():
        return all(sum(r["form"] == f for r in originals) >= n for f, n in wanted.items())

    scan(pages[0])
    while not satisfied() and overflow:
        scan(get_json(f"https://data.sec.gov/submissions/{overflow.pop(0)}"))

    kept = []
    for form, n in wanted.items():
        kept += [r for r in originals if r["form"] == form][:n]
    periods = {(r["form"], r["period"]) for r in kept}
    kept += [a for a in amendments if (a["form"].removesuffix("/A"), a["period"]) in periods]
    return sorted(kept, key=lambda r: r["filed"], reverse=True)


def main() -> int:
    print(f"User-Agent: {USER_AGENT}")
    tickers = get_json("https://www.sec.gov/files/company_tickers.json")
    lookup = {row["ticker"]: (row["cik_str"], row["title"]) for row in tickers.values()}

    # If a company fails this run, keep yesterday's numbers instead of dropping it.
    previous = {}
    if OUT_PATH.exists():
        try:
            previous = json.loads(OUT_PATH.read_text()).get("companies", {})
        except json.JSONDecodeError:
            pass

    companies, errors = {}, {}
    for t in TICKERS:
        try:
            cik, name = lookup[t]
            companies[t] = build_company(t, cik, name)
            q = companies[t]["quarters"][-1]
            print(f"{t:6} ok  latest {q['fiscal_period']} (ends {q['period_end']})")
        except Exception as e:  # one bad company shouldn't sink the whole run
            errors[t] = str(e)
            print(f"{t:6} FAILED: {e}", file=sys.stderr)
            if t in previous:
                companies[t] = previous[t]

    if not any(t not in errors for t in TICKERS):
        print("Every company failed; leaving data.json untouched.", file=sys.stderr)
        return 1

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "SEC EDGAR XBRL companyfacts API",
        "units": {"revenue": "USD", "gross_profit": "USD",
                  "operating_income": "USD", "eps_diluted": "USD per diluted share"},
        "errors": errors,
        "companies": {t: companies[t] for t in TICKERS if t in companies},
    }
    OUT_PATH.write_text(json.dumps(out, indent=2) + "\n")
    print(f"Wrote {OUT_PATH} ({len(out['companies'])} companies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
