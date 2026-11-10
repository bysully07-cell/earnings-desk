"""
targets.py: analyst price targets, from the first source that works on our plan.

Sources are tried in this order every daily run (one test call each, on AAPL):
  1. Finnhub /stock/price-target         (FINNHUB_API_KEY; premium-only on Finnhub)
  2. Financial Modeling Prep consensus    (FMP_API_KEY; free plan: 250 calls/day)
  3. Alpha Vantage OVERVIEW               (ALPHAVANTAGE_API_KEY; free plan: 25 calls/day,
     AnalystTargetPrice                    mean target only)
No Yahoo Finance, no scraping.

A source with a daily cap refreshes targets on a rotation, oldest first, and
every target keeps its own "as_of" date and source name.

Keys travel in query strings, so error text from requests (which can include
the URL) never leaves this module: callers only see the messages written here.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date

import requests

import finnhub

PROBE_SYMBOL = "AAPL"


class NoAccess(Exception):
    """The plan doesn't include this endpoint (or this symbol)."""


class LimitReached(Exception):
    """The source's daily (or per-minute) cap is used up; stop for today."""


class SourceError(Exception):
    pass


def _vendor_symbol(ticker: str) -> str:
    return ticker.replace(".", "-")          # BRK.B -> BRK-B for FMP and Alpha Vantage


class _Http:
    """Throttled GET with retries for connection errors, timeouts and 5xx."""

    def __init__(self, interval: float):
        self.interval, self._last, self.calls = interval, 0.0, 0
        self.session = requests.Session()

    def get(self, url: str, params: dict) -> requests.Response:
        problem = ""
        for delay in (0, 2, 4, 8):
            if delay:
                time.sleep(delay)
            wait = self.interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                r = self.session.get(url, params=params, timeout=(5, 20))
            except (requests.ConnectionError, requests.Timeout) as e:
                self.session.close()
                self.session = requests.Session()
                problem = type(e).__name__
                continue
            if r.status_code >= 500:
                problem = f"HTTP {r.status_code}"
                continue
            return r
        raise SourceError(f"gave up after retries ({problem})")


class FinnhubTargets:
    name, label, url = "finnhub", "Finnhub", "https://finnhub.io"
    key_env, daily_cap = "FINNHUB_API_KEY", None      # shares the Finnhub daily budget instead
    has_range, as_of_kind = True, "last updated by Finnhub"

    def enabled(self) -> bool:
        return finnhub.enabled()

    def fetch(self, ticker: str) -> dict | None:
        try:
            return finnhub.price_target(ticker)
        except finnhub.FinnhubError as e:
            if "(403)" in str(e) or "(401)" in str(e):
                raise NoAccess("no access on this plan (HTTP 403)" if "(403)" in str(e) else "key rejected (HTTP 401)")
            raise SourceError(str(e))


class FmpTargets:
    name, label, url = "fmp", "Financial Modeling Prep", "https://site.financialmodelingprep.com"
    key_env, daily_cap = "FMP_API_KEY", 240           # free plan: 250/day; keep 10 spare for manual tests
    has_range, as_of_kind = True, "retrieved"
    ENDPOINT = "https://financialmodelingprep.com/stable/price-target-consensus"

    def __init__(self):
        self.http = _Http(0.4)

    def enabled(self) -> bool:
        return bool(os.environ.get(self.key_env))

    def fetch(self, ticker: str) -> dict | None:
        r = self.http.get(self.ENDPOINT, {"symbol": _vendor_symbol(ticker), "apikey": os.environ[self.key_env]})
        try:
            body = r.json()
        except ValueError:
            raise SourceError(f"HTTP {r.status_code}, not JSON")
        msg = body.get("Error Message", "") if isinstance(body, dict) else ""
        if r.status_code == 429 or "limit" in msg.lower():
            raise LimitReached("daily limit reached")
        if r.status_code in (401, 403) or "invalid api key" in msg.lower():
            raise NoAccess(f"key rejected (HTTP {r.status_code})")
        if r.status_code == 402 or "subscription" in msg.lower() or "premium" in msg.lower() or "restricted" in msg.lower():
            raise NoAccess(f"not included in this plan (HTTP {r.status_code})")
        if r.status_code != 200 or msg:
            raise SourceError(f"HTTP {r.status_code}")
        row = body[0] if isinstance(body, list) and body else {}
        if not row.get("targetConsensus"):
            return None
        return {"mean": row["targetConsensus"], "high": row.get("targetHigh"), "low": row.get("targetLow"),
                "median": row.get("targetMedian"), "as_of": date.today().isoformat()}


class AlphaVantageTargets:
    name, label, url = "alphavantage", "Alpha Vantage", "https://www.alphavantage.co"
    key_env, daily_cap = "ALPHAVANTAGE_API_KEY", 24   # free plan: 25/day
    has_range, as_of_kind = False, "retrieved"
    ENDPOINT = "https://www.alphavantage.co/query"

    def __init__(self):
        self.http = _Http(13.0)                       # stay well under 5/minute

    def enabled(self) -> bool:
        return bool(os.environ.get(self.key_env))

    def fetch(self, ticker: str) -> dict | None:
        r = self.http.get(self.ENDPOINT, {"function": "OVERVIEW", "symbol": _vendor_symbol(ticker),
                                          "apikey": os.environ[self.key_env]})
        try:
            body = r.json()
        except ValueError:
            raise SourceError(f"HTTP {r.status_code}, not JSON")
        note = body.get("Information") or body.get("Note") or ""
        if "rate limit" in note.lower() or "requests per" in note.lower():
            raise LimitReached("daily limit reached")
        if "premium" in note.lower():
            raise NoAccess("not included in this plan")
        if "api key" in (note + body.get("Error Message", "")).lower():
            raise NoAccess("key rejected")
        if r.status_code != 200:
            raise SourceError(f"HTTP {r.status_code}")
        try:
            mean = float(body.get("AnalystTargetPrice"))
        except (TypeError, ValueError):
            return None
        return {"mean": mean, "high": None, "low": None, "median": None, "as_of": date.today().isoformat()} if mean > 0 else None


SOURCES = [FinnhubTargets, FmpTargets, AlphaVantageTargets]


def choose() -> tuple[object | None, list[dict], dict | None]:
    """Test each source in order on PROBE_SYMBOL. Returns (source, test log, probe result)."""
    tested = []
    for cls in SOURCES:
        src = cls()
        entry = {"source": src.name, "label": src.label}
        if not src.enabled():
            entry["result"] = f"skipped: no {src.key_env} secret"
            tested.append(entry)
            continue
        try:
            got = src.fetch(PROBE_SYMBOL)
        except (NoAccess, LimitReached, SourceError) as e:
            entry["result"] = str(e)
            tested.append(entry)
            continue
        if not got:
            entry["result"] = f"no target returned for {PROBE_SYMBOL}"
            tested.append(entry)
            continue
        entry["result"] = "ok"
        tested.append(entry)
        return src, tested, got
    return None, tested, None


def describe(src) -> dict:
    return {"source": src.name, "label": src.label, "url": src.url, "daily_cap": src.daily_cap,
            "has_range": src.has_range, "as_of_kind": src.as_of_kind}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)
