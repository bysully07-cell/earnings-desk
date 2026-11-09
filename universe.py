"""
universe.py: decides which companies the site tracks.

UNIVERSE=indexes (default)  S&P 500 + Nasdaq-100, scraped from Wikipedia's
                            maintained constituent tables (~516 tickers).
UNIVERSE=all                Every SEC-registered company listed on Nasdaq,
                            NYSE or CBOE (~7,700 tickers). Switching is a one-word
                            change; see the README for what that costs in Finnhub calls.

Tickers use the dot form for share classes (BRK.B), which is what Wikipedia
and Finnhub use. SEC writes them with a dash (BRK-B); sec_symbol() converts.
"""

from __future__ import annotations

import html
import os
import re
from html.parser import HTMLParser

import requests

import sec

UNIVERSE = os.environ.get("UNIVERSE", "indexes")

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_HEADERS = {"User-Agent": "EarningsDesk/1.0 (https://github.com/bysully07-cell/earnings-desk)"}
SOURCES = {
    # index code: (Wikipedia page, ticker column, name column, expected size range)
    "SPX": ("List_of_S&P_500_companies", "Symbol", "Security", (490, 515)),
    "NDX": ("List_of_NASDAQ-100_companies", "Ticker", "Company", (95, 106)),
}
LISTED_EXCHANGES = {"Nasdaq", "NYSE", "CBOE"}


def sec_symbol(ticker: str) -> str:
    return ticker.replace(".", "-")


class _TableParser(HTMLParser):
    """Collect the rows of the first <table id="constituents"> as lists of cell text."""

    def __init__(self):
        super().__init__()
        self.depth = 0          # >0 while inside the target table (handles nested tables)
        self.rows: list[list[str]] = []
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self.depth or dict(attrs).get("id") == "constituents" and not self.rows:
                self.depth += 1
        elif self.depth == 1 and tag == "tr":
            self.rows.append([])
        elif self.depth == 1 and tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self.depth:
            self.depth -= 1
        elif self.depth == 1 and tag in ("td", "th") and self.cell is not None:
            text = re.sub(r"\s+", " ", "".join(self.cell)).strip()
            text = re.sub(r"\[\d+\]", "", text)   # drop footnote markers like [2]
            self.rows[-1].append(text)
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def clean_name(name: str) -> str:
    """S&P's list writes some names back to front: "Lilly (Eli)" -> "Eli Lilly", "Hershey Company (The)" -> "The Hershey Company"."""
    name = name.strip()
    m = re.match(r"^(.*\S) \((?!Class\b)([^()]+)\)$", name)
    return f"{m.group(2)} {m.group(1)}" if m else name


def _wiki_table(page: str) -> list[dict]:
    r = requests.get(WIKI_API, headers=WIKI_HEADERS, timeout=30, params={
        "action": "parse", "page": page, "prop": "text", "format": "json",
        "formatversion": "2", "redirects": "1",
    })
    r.raise_for_status()
    p = _TableParser()
    p.feed(r.json()["parse"]["text"])
    header, *body = [row for row in p.rows if row]
    return [dict(zip(header, map(html.unescape, row))) for row in body if len(row) == len(header)]


def index_members() -> dict[str, dict]:
    """
    {ticker: {"name": ..., "indexes": [...]}} for the S&P 500 and Nasdaq-100.
    Raises if a table looks wrong (e.g. Wikipedia changed its layout), so the
    caller can keep the previous universe instead of publishing a broken one.
    """
    members: dict[str, dict] = {}
    for code, (page, tcol, ncol, (lo, hi)) in SOURCES.items():
        rows = _wiki_table(page)
        if not lo <= len(rows) <= hi:
            raise ValueError(f"{code}: expected {lo}-{hi} constituents, got {len(rows)}")
        for row in rows:
            t = row[tcol].strip().upper()
            entry = members.setdefault(t, {"name": clean_name(row[ncol]), "indexes": []})
            entry["indexes"].append(code)
    return members


def all_listed() -> dict[str, dict]:
    """Every SEC-registered company on a major US exchange."""
    data = sec.get_json("https://www.sec.gov/files/company_tickers_exchange.json")
    out = {}
    for cik, name, ticker, exchange in data["data"]:
        if exchange in LISTED_EXCHANGES and ticker:
            out[ticker.upper().replace("-", ".")] = {"name": name, "indexes": [], "cik": cik}
    return out


def load(previous: dict[str, dict] | None = None) -> dict[str, dict]:
    """Current universe with CIKs attached. Falls back to `previous` if the source fails."""
    try:
        members = all_listed() if UNIVERSE == "all" else index_members()
    except Exception as e:  # noqa: BLE001 - any failure means "keep yesterday's list"
        if previous:
            print(f"Universe lookup failed ({e}); keeping the previous {len(previous)} tickers.")
            return previous
        raise

    ciks = sec.ticker_map()
    for t, m in members.items():
        m.setdefault("cik", ciks.get(sec_symbol(t)))
    missing = [t for t, m in members.items() if not m["cik"]]
    if missing:
        print(f"No SEC CIK for {len(missing)} tickers (skipped): {', '.join(missing[:15])}")
    return {t: m for t, m in sorted(members.items()) if m["cik"]}
