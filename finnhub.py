"""
finnhub.py: a small Finnhub client shared by the daily and price jobs.

The key comes from the FINNHUB_API_KEY environment variable (a GitHub secret
in Actions). It is only ever used server-side; the website never sees it.

Free tier: 60 requests/minute. Every call goes through one throttle that
spaces requests 1.1 s apart (~54/min), and the two workflows share a
concurrency group, so they never run at the same time and double up.
"""

from __future__ import annotations

import os
import sys
import time

import requests

API = "https://finnhub.io/api/v1"
KEY = os.environ.get("FINNHUB_API_KEY", "")
MIN_INTERVAL = 1.1      # seconds between calls -> ~54/minute, under the 60/minute cap
MAX_RETRIES = 3

# Deliberately a plain session: the SEC User-Agent (which contains an email)
# is not sent to Finnhub.
_session = requests.Session()
_last = 0.0
calls = 0


class FinnhubError(Exception):
    pass


def enabled() -> bool:
    return bool(KEY)


def get(path: str, **params) -> dict | list:
    global _last, calls
    if not KEY:
        raise FinnhubError("FINNHUB_API_KEY is not set")
    for attempt in range(MAX_RETRIES):
        wait = MIN_INTERVAL - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()
        calls += 1
        r = _session.get(f"{API}{path}", params={**params, "token": KEY}, timeout=20)
        if r.status_code == 429:            # over the limit anyway: back off a full minute
            print("  Finnhub 429, pausing 60s", file=sys.stderr)
            time.sleep(60)
            continue
        if r.status_code in (401, 403):
            raise FinnhubError(f"Finnhub rejected the request ({r.status_code}); check the API key / plan")
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise FinnhubError(f"Gave up on {path} after {MAX_RETRIES} attempts")


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
