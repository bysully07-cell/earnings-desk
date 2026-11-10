#!/usr/bin/env python3
"""
update_prices.py: refresh share prices in data/index.json from Finnhub.

Scheduled every 30 minutes on weekdays; exits without changes outside US market
hours (9:30-16:00 ET, plus a short window after the close to record the closing
price) and on exchange holidays.

No extended hours: Finnhub's free /quote returns the regular-session price only.
Checked 2026-11-09: quotes fetched at 7:51-8:01pm ET (during after-hours trading)
all carried the 4:00:00pm ET timestamp and the closing price. The site labels
prices from outside the session "At close".

Budget: one Finnhub call per ticker, 50/minute, so ~520 tickers take ~11
minutes. PRICE_BUDGET caps a run; tickers with the oldest price go first, so a
bigger universe (UNIVERSE=all) still rotates through everyone.

Failures: finnhub.get() retries dropped connections, timeouts, 429 and 5xx with
backoff. A ticker that still fails is skipped (it keeps its previous price), and
whatever was fetched is always written. The run counts as failed only if more
than MAX_SKIP_SHARE of tickers were skipped.

Sets in $GITHUB_OUTPUT:
  changed=true|false    the workflow commits and deploys when true
  too_many_failed=...   the workflow marks the run failed (after deploying) when true
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import finnhub
import store

PRICE_BUDGET = 1200                      # ~24 minutes of calls; keeps each run inside the 30-minute slot
MAX_SKIP_SHARE = 0.20                    # fail the run only if more than 20% of tickers were skipped
RUN_DEADLINE = 25 * 60                   # stop fetching after 25 min so the commit/deploy always fit in the job
OUTAGE_STREAK = 10                       # this many skips in a row means Finnhub is down; stop and save
ET = ZoneInfo("America/New_York")
OPEN, LAST_RUN = dtime(9, 30), dtime(16, 45)   # 16:45 catches the close even if cron starts late


def set_output(changed: bool, too_many_failed: bool = False) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
            f.write(f"too_many_failed={'true' if too_many_failed else 'false'}\n")


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
    ap.add_argument("--min-gap", type=int, default=0, metavar="MINUTES",
                    help="skip if prices were saved less than this many minutes ago "
                         "(used by scheduled triggers so a doubled trigger doesn't run twice)")
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
    if args.min_gap and not args.force:
        last = index.get("prices_updated_at")
        if last:
            age = (datetime.now(ET) - datetime.fromisoformat(last)).total_seconds() / 60
            if age < args.min_gap:
                print(f"Skipping: prices were saved {age:.0f} min ago (another trigger already ran)")
                set_output(False)
                return 0
    rows = index.get("companies", [])
    if not rows:
        print("data/index.json is empty; run fetch_data.py first.", file=sys.stderr)
        set_output(False)
        return 1

    # Oldest prices first; share classes (GOOG/GOOGL) are separate quotes, so no dedupe here.
    order = sorted(rows, key=lambda r: r.get("price_time") or 0)[:PRICE_BUDGET]
    fetched = streak = 0
    skipped: dict[str, str] = {}          # ticker -> reason
    not_tried: list[str] = []
    stop_reason = ""
    started = time.monotonic()
    try:
        for n, r in enumerate(order, 1):
            t = r["ticker"]
            if time.monotonic() - started > RUN_DEADLINE:
                stop_reason = f"time limit ({RUN_DEADLINE // 60} min) reached"
            elif streak >= OUTAGE_STREAK:
                stop_reason = f"{streak} tickers in a row failed; Finnhub looks unavailable"
            if stop_reason:
                not_tried = [x["ticker"] for x in order[n - 1:]]
                break
            try:
                q = finnhub.quote(t)
            except finnhub.FinnhubError as e:
                skipped[t] = str(e)
                streak += 1
                print(f"  {t}: skipped ({e})", file=sys.stderr)
                if "rejected" in str(e):   # bad key: every other call would fail the same way
                    stop_reason = "API key rejected"
                    not_tried = [x["ticker"] for x in order[n:]]
                    break
                continue
            except Exception as e:  # noqa: BLE001 - one odd response must not sink the run
                skipped[t] = f"{type(e).__name__}: {e}"
                streak += 1
                print(f"  {t}: skipped ({skipped[t]})", file=sys.stderr)
                continue
            if not q:
                skipped[t] = "no quote returned"
                continue
            streak = 0
            r.update(price=q["price"], change_pct=q["change_pct"], price_time=q["time"])
            r["market_cap"] = store.market_cap(r)
            fetched += 1
            if n % 100 == 0:
                print(f"  {n}/{len(order)} done")
    finally:
        # Save whatever we have, even if something unexpected stops the loop.
        if fetched:
            index["prices_updated_at"] = store.now_iso()
            store.write_index(index)

    n_skipped = len(skipped) + len(not_tried)
    too_many = n_skipped > MAX_SKIP_SHARE * len(order)
    print(f"\nSummary: fetched {fetched}, skipped {n_skipped} of {len(order)} tickers "
          f"({finnhub.calls} Finnhub calls, {time.monotonic() - started:.0f}s)")
    if skipped:
        print("Skipped: " + ", ".join(f"{t} ({why})" for t, why in sorted(skipped.items())))
    if not_tried:
        print(f"Stopped early: {stop_reason}. Not tried ({len(not_tried)}, they keep their "
              f"previous price): " + ", ".join(sorted(not_tried)))
    if too_many:
        print(f"More than {MAX_SKIP_SHARE:.0%} of tickers were skipped; the run will be marked failed "
              "after the fetched prices are committed and deployed.", file=sys.stderr)
    set_output(fetched > 0, too_many)
    return 0   # the workflow decides pass/fail from too_many_failed, after deploying



if __name__ == "__main__":
    sys.exit(main())
