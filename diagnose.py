#!/usr/bin/env python3
"""
diagnose.py — find a request that returns SETTLED markets.

Gamma's default /markets query appears to hide closed markets, so resolved
sports fixtures come back empty and the backtest can never settle them.
This tries every plausible variant against markets we know are settled and
reports which ones work.

    python3 diagnose.py
"""
import json
from bot import polymarket as pm

GAMMA = "https://gamma-api.polymarket.com"


def try_request(label, url, params):
    rows = pm._rows(pm._get(url, params, tries=1))
    if not rows:
        return label, False, None
    m = rows[0] if isinstance(rows[0], dict) else {}
    closed = m.get("closed")
    prices = m.get("outcomePrices") or m.get("outcome_prices")
    end = m.get("endDate") or m.get("end_date_iso")
    return label, True, {"closed": closed, "outcomePrices": prices, "endDate": end}


def main():
    try:
        cache = json.load(open(".market_cache.json"))
    except Exception:
        print("No .market_cache.json — run the backtest first.")
        return 1

    unresolved = [(t, v) for t, v in cache.items() if v[0] is None
                  and len(v) > 2 and v[2]]
    print(f"{len(cache)} markets cached, {len(unresolved)} unresolved\n")
    if not unresolved:
        print("nothing to diagnose")
        return 0

    token, entry = unresolved[0]
    slug = entry[2]
    print(f"testing against: {slug}\n")

    attempts = [
        ("plain slug",            f"{GAMMA}/markets", {"slug": slug, "limit": 1}),
        ("slug + closed=true",    f"{GAMMA}/markets", {"slug": slug, "limit": 1, "closed": "true"}),
        ("slug + archived=true",  f"{GAMMA}/markets", {"slug": slug, "limit": 1, "archived": "true"}),
        ("slug + active=false",   f"{GAMMA}/markets", {"slug": slug, "limit": 1, "active": "false"}),
        ("slug, no filters",      f"{GAMMA}/markets", {"slug": slug}),
        ("path /markets/slug",    f"{GAMMA}/markets/slug/{slug}", None),
        ("events by slug",        f"{GAMMA}/events", {"slug": slug, "limit": 1}),
        ("events closed=true",    f"{GAMMA}/events", {"slug": slug, "limit": 1, "closed": "true"}),
        ("by clob token id",      f"{GAMMA}/markets", {"clob_token_ids": token, "limit": 1}),
        ("token + closed=true",   f"{GAMMA}/markets", {"clob_token_ids": token, "limit": 1, "closed": "true"}),
    ]

    winners = []
    for label, url, params in attempts:
        lbl, ok, info = try_request(label, url, params)
        if ok:
            winners.append(label)
            print(f"  WORKS   {lbl}")
            print(f"          {info}")
        else:
            print(f"  empty   {lbl}")

    # the CLOB side, which is a different service entirely
    print()
    for side in ("SELL", "BUY"):
        p = pm.fetch_price(token, side)
        print(f"  clob price {side}: {p}")
    book = pm._get("https://clob.polymarket.com/book", {"token_id": token}, tries=1)
    print(f"  clob book: {'returned data' if book else 'nothing'}")

    print()
    if winners:
        print(f"Use: {winners[0]}")
    else:
        print("Nothing returned settled data. Resolution may have to come from\n"
              "the trader's own sell prices instead of market lookups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
