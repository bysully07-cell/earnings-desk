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
  4. targets.py    Analyst price targets: Finnhub if the plan allows it, otherwise
                   FMP for the tickers its free plan covers and Alpha Vantage for
                   the rest, each on an oldest-first rotation under its daily cap.
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
            "target_sources": old.get("target_sources") or _bootstrap_target_sources(old),
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

    # --- 3. Price targets: FMP where it covers the ticker, Alpha Vantage for the rest
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


NOT_COVERED_RECHECK_DAYS = 30   # re-ask FMP about a ticker it didn't cover once a month


def _bootstrap_target_sources(old: dict) -> dict:
    """Files written before per-source tracking: an empty FMP check means 'not covered'."""
    pt, checked = old.get("price_target"), old.get("price_target_checked")
    if not checked:
        return {}
    if pt and pt.get("source") == "fmp":
        return {"fmp": {"status": "covered", "checked": checked}}
    if not pt:
        return {"fmp": {"status": "not_covered", "checked": checked}}
    return {}


def refresh_targets(companies: dict, members: dict, previous_info: dict | None) -> dict:
    """Price targets, one call per company (share classes share a target).

    1. Finnhub /stock/price-target, if this plan ever allows it: then it covers everyone.
    2. Otherwise FMP for every ticker its free plan covers. Tickers it doesn't cover are
       remembered and re-checked monthly, so the 240 daily calls go to covered tickers.
    3. Alpha Vantage (mean target only) for the tickers FMP doesn't cover, oldest first.
    Calls per source per day are counted in index.json, so a second run on the same day
    can't push a source over its daily cap.
    """
    today = date.today().isoformat()
    recheck_before = (date.today() - timedelta(days=NOT_COVERED_RECHECK_DAYS)).isoformat()
    prev = previous_info or {}
    used = prev.get("usage", {}) if prev.get("usage", {}).get("date") == today else {}
    usage = {"date": today, "fmp": used.get("fmp", 0), "alphavantage": used.get("alphavantage", 0)}

    groups: dict[int, list[str]] = {}
    for t in companies:
        groups.setdefault(members[t]["cik"], []).append(t)
    lead = {cik: ts[0] for cik, ts in groups.items()}       # the ticker we ask about
    meta = lambda cik: companies[lead[cik]]["target_sources"]  # noqa: E731

    def save(cik, src_name, got, status=None):
        m = {**meta(cik), src_name: {"status": status or ("covered" if got else "no_target"), "checked": today}}
        for t in groups[cik]:
            c = companies[t]
            c["target_sources"] = m
            if got:
                c["price_target"] = {**got, "source": src_name}
                c["price_target_checked"] = today
            elif (c.get("price_target") or {}).get("source") == src_name:
                c["price_target"] = None                       # this source dropped its coverage
                c["price_target_checked"] = today

    tested, sources_used = [], []

    def run(src, ciks: list[int], cap: int | None) -> dict:
        """Fetch targets for ciks in order until the cap or a hard stop. Returns stats."""
        st = {"refreshed": 0, "not_covered": 0, "no_target": 0, "errors": 0, "stopped": ""}
        for cik in ciks:
            if cap is not None and usage.get(src.name, 0) >= cap:
                st["stopped"] = f"daily cap of {cap} calls reached"
                break
            if src.name == "finnhub" and finnhub.calls >= FINNHUB_DAILY_BUDGET:
                st["stopped"] = "Finnhub daily budget used up"
                break
            usage[src.name] = usage.get(src.name, 0) + 1
            try:
                got = src.fetch(lead[cik])
            except targets.LimitReached as e:
                st["stopped"] = str(e)
                usage[src.name] = cap or usage[src.name]           # the source says we're out for today
                break
            except targets.NoAccess as e:
                if "key" in str(e):
                    st["stopped"] = str(e)
                    break
                save(cik, src.name, None, "not_covered")           # plan doesn't include this symbol
                st["not_covered"] += 1
                continue
            except targets.SourceError as e:
                print(f"  target {lead[cik]} ({src.label}): {e}", file=sys.stderr)
                st["errors"] += 1
                continue
            if got:
                save(cik, src.name, got)
                st["refreshed"] += 1
            elif src.name == "fmp":
                save(cik, src.name, None, "not_covered")           # free plan answers [] for symbols it doesn't cover
                st["not_covered"] += 1
            else:
                save(cik, src.name, None)
                st["no_target"] += 1
        return st

    def log(src, st, note=""):
        parts = [f"{st['refreshed']} refreshed"]
        if st["not_covered"]:
            parts.append(f"{st['not_covered']} not covered by this plan")
        if st["no_target"]:
            parts.append(f"{st['no_target']} without a target")
        if st["errors"]:
            parts.append(f"{st['errors']} errors")
        if st["stopped"]:
            parts.append(f"stopped: {st['stopped']}")
        result = ("ok: " if st["refreshed"] else "") + ", ".join(parts) + note
        tested.append({"source": src.name, "label": src.label, "result": result})
        print(f"Price targets, {src.label}: {result}")

    def skip(src, why):
        tested.append({"source": src.name, "label": src.label, "result": why})
        print(f"Price targets, {src.label}: {why}")

    by_age = lambda name: sorted(groups, key=lambda cik: (meta(cik).get(name) or {}).get("checked") or "")  # noqa: E731

    # 1. Finnhub: one test call; if it works it covers everyone.
    fh = targets.FinnhubTargets()
    fh_ok = False
    if not fh.enabled():
        skip(fh, "skipped: no FINNHUB_API_KEY secret")
    else:
        try:
            fh.fetch(targets.PROBE_SYMBOL)
            fh_ok = True
        except (targets.NoAccess, targets.LimitReached, targets.SourceError) as e:
            skip(fh, str(e))
    if fh_ok:
        st = run(fh, by_age("finnhub"), None)
        log(fh, st)
        sources_used.append(fh)
    else:
        # 2. FMP for covered (or not-yet-checked) tickers; not-covered ones only monthly.
        fmp = targets.FmpTargets()
        fmp_works = False
        if not fmp.enabled():
            skip(fmp, "skipped: no FMP_API_KEY secret")
        else:
            def fmp_due(cik):
                f = meta(cik).get("fmp") or {}
                return f.get("status") != "not_covered" or (f.get("checked") or "") < recheck_before
            st = run(fmp, [cik for cik in by_age("fmp") if fmp_due(cik)], fmp.daily_cap)
            fmp_works = bool(st["refreshed"]) or any((meta(c).get("fmp") or {}).get("status") == "covered" for c in groups)
            log(fmp, st)
            if fmp_works:
                sources_used.append(fmp)

        # 3. Alpha Vantage for everything FMP can't cover (or everyone, if FMP isn't usable).
        av = targets.AlphaVantageTargets()
        if not av.enabled():
            skip(av, "skipped: no ALPHAVANTAGE_API_KEY secret")
        else:
            def needs_av(cik):
                return not fmp_works or (meta(cik).get("fmp") or {}).get("status") == "not_covered"
            st = run(av, [cik for cik in by_age("alphavantage") if needs_av(cik)], av.daily_cap)
            log(av, st)
            if st["refreshed"] or any(((companies[lead[c]].get("price_target") or {}).get("source") == "alphavantage") for c in groups):
                sources_used.append(av)

    from collections import Counter
    by_source = Counter((c.get("price_target") or {}).get("source") for c in companies.values() if c.get("price_target"))
    print(f"Price targets: {sum(by_source.values())} of {len(companies)} tickers have one {dict(by_source)}; calls today {usage}")
    return {
        "sources": [targets.describe(s) for s in sources_used],
        "tested": tested,
        "usage": usage,
        "checked": today,
        "with_target": dict(by_source),
    }


if __name__ == "__main__":
    sys.exit(main())
