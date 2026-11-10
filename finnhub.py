"""
finnhub.py: a small Finnhub client shared by the daily and price jobs.

The key comes from the FINNHUB_API_KEY environment variable (a GitHub secret
in Actions). It is only ever used server-side; the website never sees it.

Free tier: 60 requests/minute. Every call goes through one throttle that
spaces requests 1.2 s apart (50/min), and the two workflows share a
concurrency group, so they never run at the same time and double up.
"""

from __future__ import annotations

import os
import sys
import time

import requests

API = "https://finnhub.io/api/v1"
KEY = os.environ.get("FINNHUB_API_KEY", "")
MIN_INTERVAL = 1.2      # seconds between calls -> 50/minute, a 10-call margin under the 60/minute cap
MAX_INTERVAL = 2.0      # a 429 nudges the interval up, to at most 30/minute
RETRY_DELAYS = (2, 4, 8, 16)   # exponential backoff: up to 4 retries, ~30 s worst case per ticker
RETRY_STATUS = {429, 500, 502, 503, 504}
TIMEOUT = (5, 15)       # seconds to connect, seconds to read

# Deliberately a plain session: the SEC User-Agent (which contains an email)
# is not sent to Finnhub.
_session = requests.Session()
_last = 0.0
_interval = MIN_INTERVAL
calls = 0


class FinnhubError(Exception):
    pass


def enabled() -> bool:
    return bool(KEY)


def _throttle() -> None:
    global _last, calls
    wait = _interval - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()
    calls += 1


def get(path: str, **params) -> dict | list:
    """One Finnhub GET. Retries dropped connections, timeouts, 429 and 5xx with
    exponential backoff; anything that still fails raises FinnhubError, so callers
    only have one exception type to handle."""
    global _session, _interval
    if not KEY:
        raise FinnhubError("FINNHUB_API_KEY is not set")
    last_problem = ""
    for attempt in range(len(RETRY_DELAYS) + 1):
        if attempt:
            delay = RETRY_DELAYS[attempt - 1]
            print(f"  Finnhub {path} {params.get('symbol', '')}: {last_problem}; "
                  f"retry {attempt}/{len(RETRY_DELAYS)} in {delay}s", file=sys.stderr)
            time.sleep(delay)
        _throttle()
        try:
            r = _session.get(f"{API}{path}", params={**params, "token": KEY}, timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as e:
            # A dropped keep-alive connection can leave the pool in a bad state; start fresh.
            _session.close()
            _session = requests.Session()
            last_problem = type(e).__name__
            continue
        if r.status_code in RETRY_STATUS:
            if r.status_code == 429:   # we were too fast: slow every later call down a little
                _interval = min(_interval + 0.2, MAX_INTERVAL)
            last_problem = f"HTTP {r.status_code}"
            continue
        if r.status_code in (401, 403):
            raise FinnhubError(f"Finnhub rejected the request ({r.status_code}); check the API key / plan")
        if r.status_code >= 400:
            raise FinnhubError(f"HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            last_problem = "invalid JSON"
            continue
    raise FinnhubError(f"gave up after {len(RETRY_DELAYS)} retries ({last_problem})")


def quote(symbol: str) -> dict | None:
    """Last price, day change and timestamp. None if Finnhub has no quote (c == 0)."""
    q = get("/quote", symbol=symbol)
    if not q or not q.get("c"):
        return None
    return {"price": q["c"], "change": q.get("d"), "change_pct": q.get("dp"),
            "prev_close": q.get("pc"), "time": q.get("t")}


def profile(symbol: str) -> dict:
    """Logo, industry and shares outstanding (millions)."""
    p = get("/stock/profile2", symbol=symbol) or {}
    return {"logo": p.get("logo") or None, "industry": p.get("finnhubIndustry") or None,
            "shares_m": p.get("shareOutstanding") or None}


def earnings(symbol: str) -> list[dict]:
    """Last four quarters of EPS actual vs. analyst estimate (free tier returns 4)."""
    rows = get("/stock/earnings", symbol=symbol) or []
    out = []
    for r in rows:
        actual, est = r.get("actual"), r.get("estimate")
        out.append({
            "period": r.get("period"),           # calendar quarter end, e.g. 2026-09-30
            "actual": actual,
            "estimate": est,
            "surprise_pct": r.get("surprisePercent"),
            "result": classify(actual, est),
        })
    return sorted(out, key=lambda r: r["period"] or "")


def recommendations(symbol: str, months: int = 4) -> list[dict]:
    """Analyst rating counts per month, newest first (free tier). Latest month + the 3 before it."""
    rows = get("/stock/recommendation", symbol=symbol) or []
    rows = sorted((r for r in rows if r.get("period")), key=lambda r: r["period"], reverse=True)[:months]
    return [{"period": r["period"], "strong_buy": r.get("strongBuy") or 0, "buy": r.get("buy") or 0,
             "hold": r.get("hold") or 0, "sell": r.get("sell") or 0, "strong_sell": r.get("strongSell") or 0}
            for r in rows]


def price_target(symbol: str) -> dict | None:
    """Consensus price target. A premium endpoint: the free plan answers 403."""
    p = get("/stock/price-target", symbol=symbol) or {}
    if not p.get("targetMean"):
        return None
    return {"mean": p["targetMean"], "high": p.get("targetHigh"), "low": p.get("targetLow"),
            "median": p.get("targetMedian"), "as_of": (p.get("lastUpdated") or "")[:10] or None}


def classify(actual, estimate) -> str | None:
    """Beat / miss / in line. Estimates are quoted to the cent, so 'in line' means equal to the cent."""
    if actual is None or estimate is None:
        return None
    diff = round(actual - estimate, 4)
    if abs(diff) < 0.005:
        return "inline"
    return "beat" if diff > 0 else "miss"


def market_status() -> dict:
    return get("/stock/market-status", exchange="US") or {}
