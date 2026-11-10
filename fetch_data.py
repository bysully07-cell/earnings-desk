#!/usr/bin/env python3
"""
fetch_data.py: the daily pipeline. Runs once each weekday morning in GitHub Actions.

  1. universe.py   Which companies? S&P 500 + Nasdaq-100 constituents (or all US-listed).
  2. sec.py        For each company, the last 8 quarters of revenue, gross profit,
                   operating income and diluted EPS, plus 10-K/10-Q links, from
                   SEC's bulk XBRL archives.
  3. finnhub.py    EPS actual vs. analyst estimate (beat/miss), logos, shares
                   outstanding and analyst rating counts. Rate-limited to stay
                   under 60 requests/minute.
  4. targets.py    Analyst price targets from the first source that works on our
                   plan (Finnhub, then FMP, then Alpha Vantage), on a rotation
                   when the source has a daily cap.
  5. store.py      Write data/companies/<TICKER>.json and data/index.json.

Prices are handled separately by update_prices.py (every 30 minutes in market hours).

Usage:
  python3 fetch_data.py                          # full run, SEC bulk archives
  python3 fetch_data.py --source api --only AAPL,NVDA   # quick local test
Env: SEC_USER_AGENT, FINNHUB_API_KEY, FMP_API_KEY, ALPHAVANTAGE_API_KEY (all optional),
     UNIVERSE=indexes|all
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import finnhub
import sec
import store
import targets
import universe

# Finnhub calls the daily job may spend. 50 calls/minute, so 1,800 is ~36 minutes.
# ~510 companies x (EPS surprises + ratings) plus weekly profiles is ~1,100 a day;
# the rest covers Finnhub price targets if the plan ever includes them. With
# UNIVERSE=all the job rotates through companies, oldest-checked first.
FINNHUB_DAILY_BUDGET = 1800
PROFILE_MAX_AGE_DAYS = 7       # logos/shares change rarely; refresh weekly
BULK_CACHE = Path(__file__).resolve().parent / ".cache" / "sec"


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["bulk", "api"], default="bulk",
                    help="SEC bulk zips (default) or one API call per company")
    ap.add_argument("--only", help="comma-separated tickers to process (others keep their existing files)")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    started = time.time()
    print(f"SEC User-Agent: {sec.USER_AGENT}")

    previous = {r["ticker"]: r for r in store.read_index().get("companies", [])}
    prev_universe = {t: {"name": r["name"], "indexes": r["indexes"], "cik": r["cik"]} for t, r in previous.items()}
    members = universe.load(prev_universe)
    print(f"Universe ({universe.UNIVERSE}): {len(members)} tickers")

    only = {t.strip().upper() for t in args.only.split(",")} if args.only else None
    targets = [t for t in members if not only or t in only]

    source = sec.BulkSource(BULK_CACHE) if args.source == "bulk" else sec.ApiSource()

    # --- 1. SEC fundamentals + filings (cached per CIK: GOOG and GOOGL are one company)
    by_cik: dict[int, tuple[dict | None, list, str | None]] = {}
    errors: dict[str, str] = {}
    companies: dict[str, dict] = {}
    for n, t in enumerate(targets, 1):
        m = members[t]
        cik = m["cik"]
        if cik not in by_cik:
            fundamentals, filings, note = None, [], None
            try:
                try:
                    subs = source.submissions(cik)
                    filings = sec.build_filings(cik, subs, source)
                except FileNotFoundError:
                    subs = {}
                try:
                    fundamentals = sec.build_fundamentals(source.companyfacts(cik), subs.get("fiscalYearEnd"))
                except (sec.NoFundamentals, FileNotFoundError) as e:
                    note = str(e) if isinstance(e, sec.NoFundamentals) else "no XBRL financial data on file"
                by_cik[cik] = (fundamentals, filings, note)
            except Exception as e:  # one bad company shouldn't sink the run
                errors[t] = f"{type(e).__name__}: {e}"
                print(f"{t:6} FAILED: {errors[t]}", file=sys.stderr)
                continue
        fundamentals, filings, note = by_cik[cik]

        old = store.read_company(t) or {}
        companies[t] = {
            "ticker": t, "name": m["name"], "cik": cik, "indexes": m["indexes"],
            **(fundamentals or {"fiscal_year_end": None, "sources": {}, "quarters": []}),
            "filings": filings,
            "note": note,
            # Carried over until Finnhub refreshes them below.
            "earnings": old.get("earnings", []),
            "earnings_checked": old.get("earnings_checked"),
            "ratings": old.get("ratings", []),
            "ratings_checked": old.get("ratings_checked"),
            "price_target": old.get("price_target"),
            "price_target_checked": old.get("price_target_checked"),
        }
        if n % 100 == 0:
            print(f"  SEC: {n}/{len(targets)} done ({time.time() - started:.0f}s)")
    no_fund = sum(1 for c in companies.values() if not c["quarters"])
    print(f"SEC: {len(companies)} companies, {no_fund} without US-GAAP quarterly data, {len(errors)} errors")

    # --- 2. Finnhub: profiles (logo, industry, shares) and EPS surprises
    profiles = {t: {k: previous.get(t, {}).get(k) for k in ("logo", "industry", "shares_m", "profile_updated")}
                for t in members}
    if finnhub.enabled():
        budget = FINNHUB_DAILY_BUDGET
        today = date.today().isoformat()
        stale_before = (date.today() - timedelta(days=PROFILE_MAX_AGE_DAYS)).isoformat()

        def share_by_cik(tickers, fetch, label):
            """Call Finnhub once per company (not per share class), within the budget."""
            nonlocal budget
            done: dict[int, object] = {}
            for t in tickers:
                cik = members[t]["cik"]
                if cik not in done:
                    if budget <= 0:
                        print(f"  Finnhub budget used up during {label}")
                        return
                    budget -= 1
                    try:
                        done[cik] = fetch(t)
                    except finnhub.FinnhubError as e:
                        print(f"  {label} {t}: {e}", file=sys.stderr)
                        if "rejected" in str(e):
                            return
                        continue
                    except Exception as e:  # noqa: BLE001
                        print(f"  {label} {t}: {e}", file=sys.stderr)
                        continue
                yield t, done[cik]

        need_profile = [t for t in targets if (profiles[t]["profile_updated"] or "") < stale_before]
        for t, p in share_by_cik(need_profile, finnhub.profile, "profile") or []:
            profiles[t] = {**p, "logo": p["logo"] or profiles[t]["logo"], "profile_updated": today}

        # Earnings and ratings: oldest-checked first, so a too-small budget still rotates through everyone.
        order = sorted(companies, key=lambda t: companies[t]["earnings_checked"] or "")
        for t, rows in share_by_cik(order, finnhub.earnings, "earnings") or []:
            companies[t]["earnings"] = rows
            companies[t]["earnings_checked"] = today
        order = sorted(companies, key=lambda t: companies[t]["ratings_checked"] or "")
        for t, months in share_by_cik(order, finnhub.recommendations, "ratings") or []:
            companies[t]["ratings"] = [{**m, **(store.consensus(m) or {})} for m in months]
            companies[t]["ratings_checked"] = today
        print(f"Finnhub: {finnhub.calls} calls")
    else:
        print("FINNHUB_API_KEY not set: keeping previous logos, EPS estimates and ratings.")

    # --- 3. Price targets: test sources in order, rotate oldest-first under the daily cap
    target_info = refresh_targets(companies, members, previous_info=store.read_index().get("sources", {}).get("price_targets"))

    # --- 4. Write files
    for t, c in companies.items():
        store.write_company(t, c)
    if not only:  # drop files for companies that left the universe
        for p in store.COMPANY_DIR.glob("*.json"):
            if p.stem not in members:
                p.unlink()
                print(f"Removed {p.stem} (no longer in the universe)")

    rows = []
    for t, m in members.items():
        c = companies.get(t) or store.read_company(t)
        if c is None:
            continue
        old = previous.get(t, {})
        q = c["quarters"][-1] if c["quarters"] else {}
        yoy = None
        if q:  # match the same quarter a year earlier by date, not position (a quarter may be missing)
            end = date.fromisoformat(q["period_end"])
            prior = [p for p in c["quarters"]
                     if abs((end - date.fromisoformat(p["period_end"])).days - 365) <= 20 and p["revenue"]]
            if prior:
                yoy = q["revenue"] / prior[0]["revenue"] - 1
        latest_eps = c["earnings"][-1] if c.get("earnings") else None
        latest_rating = (c.get("ratings") or [None])[0]
        pt = c.get("price_target")
        row = {
            "ticker": t, "name": m["name"], "cik": m["cik"], "indexes": m["indexes"],
            **profiles.get(t, {}),
            "fiscal_period": q.get("fiscal_period"), "period_end": q.get("period_end"),
            "revenue": q.get("revenue"), "revenue_yoy": round(yoy, 4) if yoy is not None else None,
            "eps_diluted": q.get("eps_diluted"),
            "eps_surprise": latest_eps,
            "rating": {"period": latest_rating["period"], "counts": [latest_rating[k] for k in store.RATING_KEYS],
                       **(store.consensus(latest_rating) or {})} if latest_rating else None,
            "target": {k: pt.get(k) for k in ("mean", "high", "low", "as_of", "source")} if pt else None,
            # Prices are owned by update_prices.py; carry them over untouched.
            **{k: old.get(k) for k in ("price", "change_pct", "price_time")},
        }
        row["market_cap"] = store.market_cap(row)
        rows.append(row)

    old_index = store.read_index()
    store.write_index({
        "generated_at": store.now_iso(),
        "prices_updated_at": old_index.get("prices_updated_at"),
        "universe": universe.UNIVERSE,
        "index_names": store.INDEX_NAMES,
        "sources": {
            "ratings": {"label": "Finnhub", "url": "https://finnhub.io", "endpoint": "/stock/recommendation"},
            "price_targets": target_info,
        },
        "errors": errors,
        "companies": rows,
    })
    print(f"Wrote {store.INDEX_PATH} ({len(rows)} companies) in {time.time() - started:.0f}s")
    return 0 if len(errors) < max(5, len(targets) // 10) else 1


def refresh_targets(companies: dict, members: dict, previous_info: dict | None) -> dict:
    """Pick the first price-target source that works today, then refresh as many
    companies as its daily cap allows, oldest-checked first (once per CIK)."""
    today = date.today().isoformat()
    src, tested, probe = targets.choose()
    for t in tested:
        print(f"Price targets: {t['label']}: {t['result']}")
    if not src:
        print("Price targets: no source available; keeping previous targets.")
        info = dict(previous_info or {})
        info.update(tested=tested, checked=today, active=False)
        return info

    def save(t, got):
        cik = members[t]["cik"]
        for u, c in companies.items():          # share classes (GOOG/GOOGL) get the same company target
            if members[u]["cik"] == cik:
                c["price_target"] = {**got, "source": src.name} if got else None
                c["price_target_checked"] = today

    if targets.PROBE_SYMBOL in companies:
        save(targets.PROBE_SYMBOL, probe)
    cap = (src.daily_cap - 1) if src.daily_cap else None    # the probe used one call
    if src.name == "finnhub":
        cap = None                                           # bounded by the Finnhub run below
    done_ciks = {members[targets.PROBE_SYMBOL]["cik"]} if targets.PROBE_SYMBOL in members else set()
    order = sorted(companies, key=lambda t: companies[t].get("price_target_checked") or "")
    fetched = empty = 0
    stopped = ""
    for t in order:
        cik = members[t]["cik"]
        if cik in done_ciks:
            continue
        if cap is not None and len(done_ciks) - 1 >= cap:
            stopped = f"daily cap of {src.daily_cap} calls reached"
            break
        if src.name == "finnhub" and finnhub.calls >= FINNHUB_DAILY_BUDGET:
            stopped = "Finnhub daily budget used up"
            break
        done_ciks.add(cik)
        try:
            got = src.fetch(t)
        except targets.LimitReached as e:
            stopped = str(e)
            break
        except targets.NoAccess as e:
            if "key" in str(e):
                stopped = str(e)
                break
            save(t, None)                                    # symbol not on this plan: move on
            print(f"  target {t}: {e}", file=sys.stderr)
            continue
        except targets.SourceError as e:
            print(f"  target {t}: {e}", file=sys.stderr)
            companies[t]["price_target_checked"] = today      # keep the old target; don't retry it first tomorrow
            continue
        save(t, got)
        fetched += got is not None
        empty += got is None
    have = sum(1 for c in companies.values() if c.get("price_target"))
    print(f"Price targets via {src.label}: {fetched + (probe is not None)} refreshed, {empty} without coverage"
          f"{'; stopped: ' + stopped if stopped else ''}. {have} of {len(companies)} tickers have a target.")
    return {**targets.describe(src), "tested": tested, "checked": today, "active": True,
            "refreshed_today": fetched + 1, "with_target": have}


if __name__ == "__main__":
    sys.exit(main())
