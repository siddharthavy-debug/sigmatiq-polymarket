"""
crypto/refresh.py — keep the winners, replace the losers, leave everyone else
alone.

Every trader currently pinned in data/crypto_traders.json is scored on their
REAL performance inside this bot's own trade log (data/crypto_trades.json) --
not a fresh outside ranking. Anyone net-positive stays pinned, untouched, in
whatever slot they're in. Anyone net-negative is a candidate to be swapped
out -- but only swapped, never just dropped: a replacement is pulled from a
fresh harvest + the same LIST B filter crypto/weekly.py uses (margin >= 3%,
z >= 2, 300+ trades, 35-65c entries), and only takes that trader's place if
one actually clears the bar. If no qualifying replacement is found, the
underperformer stays exactly where they are rather than shrinking the roster.

    python3 -m crypto.refresh              # needs crypto_candidates.json
                                            # from a recent harvest run
    python3 -m crypto.refresh --dry-run    # print the plan, write nothing

Meant to be run on a schedule (see .github/workflows/crypto-refresh.yml),
right after a fresh `python3 -m crypto.harvest 30`.
"""
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import feed
from .rank_history import market_facts, history, load_cache, save_cache, THREADS
from .weekly import (
    measure, CANDIDATES, POOL_MIN_TRADES, POOL_MIN_MARKETS, POOL_MAX_PER_MKT,
    POOL_LIMIT, MIN_TRADES, MARGIN_BAR, MIN_Z, PIN_MIN_ENTRY, PIN_MAX_ENTRY,
    PIN_MIN_TRADES, MAX_BOTH_SIDES, WINDOW_DAYS,
)

TRADERS_FILE = "data/crypto_traders.json"
TRADES_FILE = "data/crypto_trades.json"


def real_pnl_by_wallet():
    """
    What each currently-pinned trader has actually earned/lost inside this
    bot's own paper (or live) run, from data/crypto_trades.json. settle
    events only carry a display 'trader' name, not a wallet, so we build the
    name -> wallet map from 'copy' events first.
    """
    try:
        with open(TRADES_FILE) as fh:
            trades = json.load(fh)
    except FileNotFoundError:
        return {}

    name_to_wallet = {}
    for t in trades:
        if t.get("event") == "copy" and t.get("trader") and t.get("wallet"):
            name_to_wallet[t["trader"]] = t["wallet"].lower()

    pnl = defaultdict(lambda: {"pnl": 0.0, "n": 0, "wins": 0})
    for t in trades:
        if t.get("event") != "settle":
            continue
        name = t.get("trader")
        wallet = name_to_wallet.get(name, name)  # fall back to name as key
        pnl[wallet]["pnl"] += t.get("pnl", 0.0)
        pnl[wallet]["n"] += 1
        if t.get("result") == "WON":
            pnl[wallet]["wins"] += 1
    return pnl


def find_replacements(exclude_wallets, needed, since):
    """Harvest-pool candidates that clear the same bar crypto/weekly.py
    uses for --pin (list B), excluding anyone already pinned."""
    load_cache()
    try:
        with open(CANDIDATES) as fh:
            cands = json.load(fh)
    except FileNotFoundError:
        print(f"No {CANDIDATES} -- run `python3 -m crypto.harvest 30` first. "
              "Skipping replacement search; nobody will be swapped this run.")
        return []

    pool = []
    for c in cands:
        n = c.get("trades_seen", 0)
        m = c.get("markets_seen", 0) or 1
        if n < POOL_MIN_TRADES or m < POOL_MIN_MARKETS:
            continue
        if n / m > POOL_MAX_PER_MKT:
            continue
        if (c.get("wallet") or "").lower() in exclude_wallets:
            continue
        pool.append(c)
    pool.sort(key=lambda c: -c.get("trades_seen", 0))
    pool = pool[:POOL_LIMIT]

    measured = []
    for c in pool:
        wallet = c["wallet"]
        name = c.get("name") or wallet[:10]
        try:
            rows = history(wallet, 3000)
        except Exception:
            continue
        slugs = {r.get("slug") or r.get("eventSlug") for r in rows}
        slugs = {s for s in slugs if s and feed.classify(s)}
        with ThreadPoolExecutor(max_workers=THREADS) as ex:
            list(ex.map(market_facts, slugs))
        m = measure(wallet, name, rows, since)
        if m is None:
            continue
        save_cache()
        if (m["trades"] >= MIN_TRADES
                and m["both_sides_rate"] <= MAX_BOTH_SIDES
                and m["margin"] >= MARGIN_BAR
                and m["edge_z"] >= MIN_Z
                and m["profit"] > 0
                and PIN_MIN_ENTRY <= m["avg_entry"] <= PIN_MAX_ENTRY
                and m["trades"] >= PIN_MIN_TRADES):
            measured.append(m)

    measured.sort(key=lambda m: -m["edge_z"])
    return measured[:needed]


def main():
    dry_run = "--dry-run" in sys.argv

    with open(TRADERS_FILE) as fh:
        book = json.load(fh)
    current = book.get("traders", [])
    if not current:
        print("Nothing pinned yet -- refresh has nothing to work from. "
              "Run crypto.weekly --pin once first.")
        return 1

    pnl = real_pnl_by_wallet()

    keep, drop = [], []
    for r in current:
        key = (r.get("wallet") or "").lower()
        real = pnl.get(key) or pnl.get(r.get("name"))
        if real is None or real["n"] < 5:
            # too little real history yet to judge -- leave alone
            keep.append(r)
            continue
        if real["pnl"] >= 0:
            keep.append(r)
        else:
            drop.append((r, real))

    print(f"{len(current)} currently pinned: {len(keep)} net-positive (kept), "
          f"{len(drop)} net-negative (up for replacement)")
    for r, real in drop:
        print(f"  - {r['name']:<20} {real['n']:>4} trades  pnl {real['pnl']:+.2f}")

    since = time.time() - WINDOW_DAYS * 86400
    exclude = {r.get("wallet").lower() for r in current if r.get("wallet")}
    replacements = find_replacements(exclude, len(drop), since) if drop else []

    new_traders = list(keep)
    replaced_names = []
    for i, (old, _real) in enumerate(drop):
        if i < len(replacements):
            m = replacements[i]
            new_traders.append({
                "wallet": m["wallet"], "name": m["name"],
                "win_rate": round(m["win_rate"], 4),
                "avg_entry": round(m["avg_entry"], 4),
                "margin": round(m["margin"], 4),
                "profit": round(m["profit"], 2),
                "trades": m["trades"],
            })
            replaced_names.append(f"{old['name']} -> {m['name']}")
        else:
            # no qualifying replacement found -- leave them pinned rather
            # than just shrinking the roster
            new_traders.append(old)

    if replaced_names:
        print("\nReplacing:")
        for line in replaced_names:
            print(f"  {line}")
    else:
        print("\nNo qualifying replacements found this run -- roster unchanged "
              "in practice (underperformers kept, nobody new cleared the bar).")

    if dry_run:
        print("\n--dry-run: not writing data/crypto_traders.json")
        return 0

    book = {
        "pinned": True,
        "list": "refresh",
        "updated": datetime.now(timezone.utc).isoformat(),
        "window_days": WINDOW_DAYS,
        "traders": new_traders,
    }
    os.makedirs("data", exist_ok=True)
    with open(TRADERS_FILE, "w") as fh:
        json.dump(book, fh, indent=1)
    print(f"\nWrote {len(new_traders)} traders -> {TRADERS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

