"""
store.py: reading and writing the site's data files.

  data/index.json              one row per company: identity, logo, latest
                               quarter, latest EPS beat/miss, live price.
                               Small enough to load on page open (~250 KB).
  data/companies/<TICKER>.json full detail for one company (8 quarters,
                               filings, EPS surprises). Loaded when you open it.

index.json is written one company per line, so a git diff of a price update
reads like a table of what changed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
COMPANY_DIR = DATA_DIR / "companies"
INDEX_PATH = DATA_DIR / "index.json"

INDEX_NAMES = {"SPX": "S&P 500", "NDX": "Nasdaq-100"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def company_path(ticker: str) -> Path:
    return COMPANY_DIR / f"{ticker}.json"


def read_index() -> dict:
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"companies": []}


def write_index(index: dict) -> None:
    rows = index.pop("companies")
    head = json.dumps(index, separators=(",", ":"))[:-1]   # drop the closing brace
    body = ",\n".join(json.dumps(r, separators=(",", ":")) for r in rows)
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(f'{head},"companies":[\n{body}\n]}}\n')
    index["companies"] = rows


def read_company(ticker: str) -> dict | None:
    p = company_path(ticker)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return None
    return None


def write_company(ticker: str, data: dict) -> None:
    COMPANY_DIR.mkdir(parents=True, exist_ok=True)
    company_path(ticker).write_text(json.dumps(data, indent=1) + "\n")


def market_cap(row: dict) -> float | None:
    if row.get("price") and row.get("shares_m"):
        return round(row["price"] * row["shares_m"] * 1e6)
    return None


# ---------- analyst ratings ----------
RATING_KEYS = ("strong_buy", "buy", "hold", "sell", "strong_sell")


def consensus(month: dict | None) -> dict | None:
    """Average rating on a 1-5 scale (1 = Strong Buy, 5 = Strong Sell), the usual
    way brokers' votes are combined, then bucketed into a label:
    <=1.5 Strong Buy, <=2.5 Buy, <=3.5 Hold, otherwise Sell."""
    if not month:
        return None
    counts = [month.get(k) or 0 for k in RATING_KEYS]
    n = sum(counts)
    if not n:
        return None
    score = sum(w * c for w, c in zip(range(1, 6), counts)) / n
    label = "Strong Buy" if score <= 1.5 else "Buy" if score <= 2.5 else "Hold" if score <= 3.5 else "Sell"
    return {"score": round(score, 2), "consensus": label, "analysts": n}
