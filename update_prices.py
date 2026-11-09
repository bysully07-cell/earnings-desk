#!/usr/bin/env python3
"""
update_prices.py: refresh share prices in data/index.json from Finnhub.

Scheduled every 30 minutes on weekdays; exits without changes outside US market
hours (9:30-16:00 ET, plus a short window after the close to record the closing
price) and on exchange holidays.

Budget: one Finnhub call per ticker, ~54/minute, so ~520 tickers take ~10
minutes. PRICE_BUDGET caps a run; tickers with the oldest price go first, so a
bigger universe (UNIVERSE=all) still rotates through everyone.

Sets changed=true|false in $GITHUB_OUTPUT so the workflow knows whether to commit.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import finnhub
import store

PRICE_BUDGET = 1400                      # ~26 minutes of calls; keeps each run inside the 30-minute slot
ET = ZoneInfo("America/New_York")
OPEN, LAST_RUN = dtime(9, 30), dtime(16, 45)   # 16:45 catches the close even if cron starts late


def set_output(changed: bool) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")


def market_window_open() -> tuple[bool, str]:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False, "weekend"
    if not OPEN <= now.time() <= LAST_RUN:
        return False, f"outside market hours ({now:%H:%M} ET)"
    try:
        status = finnhub.market_status()
        if status.get("holiday"):
            return False, f"market holiday: {status['holiday']}"
    except Exception as e:  # noqa: BLE001 - if the check fails, assume a normal day
        print(f"market-status check failed ({e}); continuing")
    return True, "open"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="run even outside market hours")
    args = ap.parse_args()

    if not finnhub.enabled():
        print("FINNHUB_API_KEY not set; nothing to do.")
        set_output(False)
        return 0
    is_open, why = market_window_open()
    if not is_open and not args.force:
        print(f"Skipping: {why}")
        set_output(False)
        return 0

    index = store.read_index()
    rows = index.get("companies", [])
    if not rows:
        print("data/index.json is empty; run fetch_data.py first.", file=sys.stderr)
        set_output(False)
        return 1

    # Oldest prices first; share classes (GOOG/GOOGL) are separate quotes, so no dedupe here.
    order = sorted(rows, key=lambda r: r.get("price_time") or 0)[:PRICE_BUDGET]
    updated = failed = 0
    for r in order:
        try:
            q = finnhub.quote(r["ticker"])
        except finnhub.FinnhubError as e:
            print(f"  {r['ticker']}: {e}", file=sys.stderr)
            failed += 1
            if "rejected" in str(e):
                break
            continue
        if q:
            r.update(price=q["price"], change_pct=q["change_pct"], price_time=q["time"])
            r["market_cap"] = store.market_cap(r)
            updated += 1
        else:
            failed += 1

    if updated:
        index["prices_updated_at"] = store.now_iso()
        store.write_index(index)
    print(f"Prices: {updated} updated, {failed} failed, {finnhub.calls} Finnhub calls")
    set_output(updated > 0)
    return 0 if updated or not order else 1


if __name__ == "__main__":
    sys.exit(main())
