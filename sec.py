"""
sec.py: everything that talks to SEC EDGAR or interprets its XBRL data.

Two interchangeable data sources:
  * BulkSource: SEC's nightly bulk zips (companyfacts.zip ~1.4 GB,
    submissions.zip ~1.6 GB). One download covers every filer, so cost doesn't
    grow with the number of companies. Used by the daily GitHub Actions job.
  * ApiSource: one HTTP request per company to data.sec.gov. Handy for local
    runs on a few tickers (fetch_data.py --source api --only AAPL,NVDA).
Both return the exact same JSON shapes, so the extraction code doesn't care.
"""

from __future__ import annotations

import os
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import requests

# SEC asks every automated client to identify itself with a name and email:
# https://www.sec.gov/os/accessing-edgar-data
USER_AGENT = os.environ.get("SEC_USER_AGENT", "Benton Sullivan bysully07@gmail.com")

# SEC's fair-access limit is 10 requests/second. We stay well under it.
MIN_SECONDS_BETWEEN_REQUESTS = 0.2   # ~5 requests/second
MAX_RETRIES = 4

COMPANYFACTS_ZIP_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
SUBMISSIONS_ZIP_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"

NUM_QUARTERS = 8
FILING_QUOTA = {"annual": 5, "quarterly": 8}           # filing links kept per company
ANNUAL_FORMS = {"10-K", "20-F", "40-F"}                # 20-F/40-F: foreign filers
QUARTERLY_FORMS = {"10-Q"}

# Companies tag the same concept differently. Each list is in priority order;
# pick_tag() also skips tags a company stopped using years ago.
REVENUE_TAGS = [
    "Revenues",  # total revenue incl. non-contract items (TSLA leasing, WMT membership)
    "RevenueFromContractWithCustomerExcludingAssessedTax",  # ASC 606 standard tag
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "RevenuesNetOfInterestExpense",  # banks and brokers
    "RegulatedAndUnregulatedOperatingRevenue",  # utilities
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
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]
# Banks have no single revenue line: revenue = net interest income + noninterest income.
NET_INTEREST_TAGS = ["InterestIncomeExpenseNet", "InterestRevenueExpenseNet"]
NONINTEREST_TAGS = ["NoninterestIncome"]
STALE_DAYS = 120   # revenue this far behind the company's other data means its tag went stale

# A "quarter" is a ~13-week duration and a "year" is a ~52/53-week duration.
# Ranges allow for 14-week quarters and 53-week years (AAPL, AVGO, AMD, WMT...).
QUARTER_DAYS = range(80, 106)
ANNUAL_DAYS = range(350, 380)


class NoFundamentals(Exception):
    """The company has no usable US-GAAP quarterly data (e.g. a 20-F filer)."""


# ---------------------------------------------------------------------------
# HTTP: polite, rate-limited, retrying
# ---------------------------------------------------------------------------

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
_last_request = 0.0


def _throttle() -> None:
    global _last_request
    wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def get_json(url: str) -> dict:
    for attempt in range(MAX_RETRIES):
        _throttle()
        resp = session.get(url, timeout=60)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            raise FileNotFoundError(url)
        if resp.status_code in (429, 500, 502, 503, 504):
            backoff = 2 ** attempt
            print(f"  {resp.status_code} from SEC, retrying in {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Gave up on {url} after {MAX_RETRIES} attempts")


def download(url: str, dest: Path) -> Path:
    """Stream a large file to disk (bulk zips are >1 GB, so never hold them in memory)."""
    _throttle()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with session.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    return dest


def ticker_map() -> dict[str, int]:
    """Every SEC-registered ticker -> CIK. SEC writes class shares with a dash (BRK-B)."""
    rows = get_json("https://www.sec.gov/files/company_tickers.json").values()
    return {r["ticker"].upper(): r["cik_str"] for r in rows}


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def _cik_name(cik: int) -> str:
    return f"CIK{cik:010d}.json"


class ApiSource:
    """One request per company. Fine for a handful of tickers."""

    def companyfacts(self, cik: int) -> dict:
        return get_json(f"https://data.sec.gov/api/xbrl/companyfacts/{_cik_name(cik)}")

    def submissions(self, cik: int) -> dict:
        return get_json(f"https://data.sec.gov/submissions/{_cik_name(cik)}")

    def submissions_page(self, name: str) -> dict:
        return get_json(f"https://data.sec.gov/submissions/{name}")


class BulkSource:
    """
    SEC's nightly bulk archives. Each zip holds one JSON per company, named
    CIK##########.json, with exactly the same structure as the per-company API.
    zipfile reads the archive's table of contents and decompresses only the
    members we ask for, so we never unpack all ~900k files.
    """

    def __init__(self, cache_dir: Path):
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.facts_zip = self._open(COMPANYFACTS_ZIP_URL, cache_dir / "companyfacts.zip")
        self.subs_zip = self._open(SUBMISSIONS_ZIP_URL, cache_dir / "submissions.zip")

    @staticmethod
    def _open(url: str, path: Path) -> zipfile.ZipFile:
        # Reuse a download from the last 20 hours (handy when re-running locally).
        fresh = path.exists() and time.time() - path.stat().st_mtime < 20 * 3600
        if not fresh:
            print(f"Downloading {url} ...")
            t0 = time.time()
            download(url, path)
            print(f"  {path.stat().st_size / 1e9:.2f} GB in {time.time() - t0:.0f}s")
        return zipfile.ZipFile(path)

    def _read(self, zf: zipfile.ZipFile, name: str) -> dict:
        import json
        try:
            with zf.open(name) as f:
                return json.load(f)
        except KeyError:
            raise FileNotFoundError(name) from None

    def companyfacts(self, cik: int) -> dict:
        return self._read(self.facts_zip, _cik_name(cik))

    def submissions(self, cik: int) -> dict:
        return self._read(self.subs_zip, _cik_name(cik))

    def submissions_page(self, name: str) -> dict:
        return self._read(self.subs_zip, name)


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


def _fye_after(q_end: date, mmdd: str) -> date:
    """The first fiscal-year end on or after q_end, given SEC's fiscalYearEnd ("0926", "1231")."""
    import calendar
    m, dd = int(mmdd[:2]), int(mmdd[2:])
    for year in (q_end.year, q_end.year + 1):
        fye = date(year, m, min(dd, calendar.monthrange(year, m)[1]))
        if fye >= q_end - timedelta(days=7):      # 52/53-week years land a few days either side
            return fye
    return fye


def fiscal_label(q_end: date, annual_ends: list[date], fye_mmdd: str | None = None) -> tuple[int, int]:
    """
    (fiscal_year, quarter_number) for a quarter, using the company's own
    fiscal calendar. NVDA's FY2026 ends Jan 2026, AAPL's ends Sept 2026, etc.
    Fiscal year is named after the calendar year it ends in.
    """
    prior = [e for e in annual_ends if e < q_end]
    if not prior:
        if not fye_mmdd:
            return q_end.year, 0
        # No annual figures on file: count quarters back from the fiscal year-end SEC lists.
        fye = _fye_after(q_end, fye_mmdd)
        months = (fye.year - q_end.year) * 12 + fye.month - q_end.month
        return fye.year, max(1, min(4, 4 - round(months / 3)))
    fy_start = max(prior)
    q_num = max(1, min(4, round((q_end - fy_start).days / 91.3)))
    fy_end = fy_start + timedelta(days=364)
    return fy_end.year, q_num


def build_fundamentals(companyfacts: dict, fye_mmdd: str | None = None) -> dict:
    """Last NUM_QUARTERS fiscal quarters of revenue, gross profit, operating income and diluted EPS."""
    facts = companyfacts.get("facts", {})

    rev, rev_d, rev_tag = quarterly_series(facts, REVENUE_TAGS, "USD")

    # How current is this company's data overall? (Net income and EPS are near-universal.)
    ni_q, _, _ = quarterly_series(facts, ["NetIncomeLoss"], "USD")
    eps_probe, _, _ = quarterly_series(facts, EPS_TAGS, "USD/shares")
    company_latest = max([*ni_q, *eps_probe, *rev], default=None)

    def stale(series):
        return not series or (company_latest - max(series)).days > STALE_DAYS

    if stale(rev):
        nii, nii_d, _ = quarterly_series(facts, NET_INTEREST_TAGS, "USD")
        nonii, nonii_d, _ = quarterly_series(facts, NONINTEREST_TAGS, "USD")
        bank = {e: (nii[e][0], nii[e][1] + nonii[e][1]) for e in nii if e in nonii}
        if bank and not stale(bank):
            rev, rev_d = bank, set(bank)          # every quarter is a computed sum
            rev_tag = "derived: net interest income + noninterest income"
        elif rev:
            raise NoFundamentals(f"revenue tags stop at {max(rev)}; newer filings use tags this site doesn't map")
    gp, gp_d, gp_tag = quarterly_series(facts, GROSS_PROFIT_TAGS, "USD")
    cost, cost_d, cost_tag = quarterly_series(facts, COST_TAGS, "USD")
    op, op_d, op_tag = quarterly_series(facts, OPERATING_INCOME_TAGS, "USD")
    pre, _, _ = quarterly_series(facts, PRETAX_TAGS, "USD")
    nonop, _, _ = quarterly_series(facts, NONOPERATING_TAGS, "USD")
    eps, eps_d, eps_tag = quarterly_series(facts, EPS_TAGS, "USD/shares")

    if not rev:
        raise NoFundamentals("no US-GAAP quarterly revenue (foreign filer or non-standard tags)")

    # Ignore tags that went stale (e.g. AMZN's GrossProfit ends in 2009).
    newest = max(rev)

    def fresh(series):
        return bool(series) and (newest - max(series)).days < 200
    gp_method = "reported" if fresh(gp) else ("revenue - cost of revenue" if fresh(cost) else None)
    op_method = "reported" if fresh(op) else (
        "pre-tax income - non-operating income" if fresh(pre) and fresh(nonop) else None)

    # Fiscal-year ends, from any of these tags (XOM, for one, doesn't tag annual revenue).
    annual_ends = sorted({e for tag in [*REVENUE_TAGS, "NetIncomeLoss"]
                          for (s, e) in durations(facts, tag, "USD") if (e - s).days in ANNUAL_DAYS})

    quarters = []
    for end in sorted(rev, reverse=True)[:NUM_QUARTERS]:
        start, revenue = rev[end]
        derived = []          # fields computed rather than read straight from a filing
        if end in rev_d:
            derived.append("revenue")

        gross = None
        if gp_method == "reported" and end in gp:
            gross = gp[end][1]
            if end in gp_d:
                derived.append("gross_profit")
        elif gp_method and end in cost:
            gross = revenue - cost[end][1]
            derived.append("gross_profit")

        operating = None
        if op_method == "reported" and end in op:
            operating = op[end][1]
            if end in op_d:
                derived.append("operating_income")
        elif op_method and end in pre and end in nonop:
            operating = pre[end][1] - nonop[end][1]
            derived.append("operating_income")

        eps_val = eps[end][1] if end in eps else None
        if end in eps_d:
            derived.append("eps_diluted")
            better = q4_eps_from_components(facts, end)
            if better is not None:
                eps_val = better

        fy, qn = fiscal_label(end, annual_ends, fye_mmdd)
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

    quarters.reverse()  # oldest -> newest, the natural order for charts
    return {
        "fiscal_year_end": annual_ends[-1].strftime("%m-%d") if annual_ends else None,
        "sources": {
            "revenue": rev_tag,
            "gross_profit": gp_tag if gp_method == "reported" else f"derived: {gp_method} ({cost_tag})" if gp_method else None,
            "operating_income": op_tag if op_method == "reported" else f"derived: {op_method}" if op_method else None,
            "eps_diluted": eps_tag,
        },
        "quarters": quarters,
    }


# ---------------------------------------------------------------------------
# Filing links
# ---------------------------------------------------------------------------

def build_filings(cik: int, submissions: dict, source) -> list[dict]:
    """
    Recent annual (10-K / 20-F / 40-F) and quarterly (10-Q) filings, newest first.

    "recent" holds only a company's latest ~1,000 filings of every type.
    Big companies file lots of insider-trading Form 4s, so older 10-Ks can
    spill into overflow pages listed under filings.files; read those as needed.
    Amendments (10-K/A) don't count toward the quota but are kept alongside
    the original they amend.
    """
    filings = submissions.get("filings", {})
    overflow = [f["name"] for f in filings.get("files", [])]
    originals, amendments = [], []

    def kind(form: str) -> str | None:
        base = form.removesuffix("/A")
        return "annual" if base in ANNUAL_FORMS else "quarterly" if base in QUARTERLY_FORMS else None

    def scan(page: dict) -> None:
        for i, form in enumerate(page.get("form", [])):
            if not kind(form):
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

    def satisfied() -> bool:
        return all(sum(kind(r["form"]) == k for r in originals) >= n for k, n in FILING_QUOTA.items())

    scan(filings.get("recent", {}))
    while not satisfied() and overflow:
        try:
            scan(source.submissions_page(overflow.pop(0)))
        except FileNotFoundError:
            break

    kept = []
    for k, n in FILING_QUOTA.items():
        kept += [r for r in originals if kind(r["form"]) == k][:n]
    periods = {(r["form"], r["period"]) for r in kept}
    kept += [a for a in amendments if (a["form"].removesuffix("/A"), a["period"]) in periods]
    return sorted(kept, key=lambda r: r["filed"], reverse=True)
