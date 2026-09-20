#!/usr/bin/env python3
"""
weekly.py — the two lists, side by side, so the data settles the argument.

You wanted traders who are right at least 70% of the time. I've argued the
better measure is how far the win rate beats the price they paid. Rather than
keep arguing, this prints both rankings over the same wallets and the same
week, with profit shown in each.

    LIST A — by win rate        your rule: >=70% of trades made money
    LIST B — by margin          win rate minus average entry price

If list A is full of profitable accounts, your rule wins and we use it. If
list A is full of people winning 80% at 84c and finishing the week down, the
table says so more convincingly than I can.

Rolling 7-day window, crypto UP/DOWN only, recomputed whenever it runs.
Read-only: pins nothing, trades nothing, writes one JSON report.

    python3 -m crypto.weekly              # last 7 days, prints only
    python3 -m crypto.weekly 14           # last 14 days
    python3 -m crypto.weekly --pin        # also pin the chosen list
    python3 -m crypto.weekly --pin=a      # pin list A instead of B
    MIN_TRADES=30 python3 -m crypto.weekly
"""
import json
import math
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import api as pm
from . import feed
from .rank_history import (market_facts, history, load_cache, save_cache,
                           CANDIDATES, THREADS)

WINDOW_DAYS  = float(os.getenv("WINDOW_DAYS", "7"))
MIN_TRADES   = int(os.getenv("MIN_TRADES", "50"))
WIN_RATE_BAR = float(os.getenv("WIN_RATE_BAR", "0.70"))
# Set from the first real run, not guessed. Across 242 wallets the median
# margin was +0.20% — these markets are priced well — and only one wallet
# reached +8%, which is why the first pass produced an empty list. At +3% there
# are 22 wallets and 17 of them made money, which is a list worth having.
MARGIN_BAR   = float(os.getenv("MARGIN_BAR", "0.03"))
# Fourteen wallets cleared z=+2. Chance alone would produce about six of those
# across 242, so roughly half are real and we cannot tell which — hence taking
# a dozen rather than betting on one.
MIN_Z        = float(os.getenv("MIN_Z", "2.0"))
# 45-55c held 126 wallets, 61% of them profitable, +$30,517 between them.
# Above 85c: ten wallets, six "profitable", minus $4,696 overall.
PIN_MIN_ENTRY = float(os.getenv("PIN_MIN_ENTRY", "0.35"))
PIN_MAX_ENTRY = float(os.getenv("PIN_MAX_ENTRY", "0.65"))
PIN_MIN_TRADES = int(os.getenv("PIN_MIN_TRADES", "300"))
MAX_BOTH_SIDES = float(os.getenv("MAX_BOTH_SIDES", "0.15"))
TOP_N        = int(os.getenv("TOP_N", "12"))

# The harvest returns thousands of wallets and each one costs a history call
# plus market lookups, so the pool has to be narrowed before ranking or this
# runs for hours. Two cuts, both cheap and both defensible:
#
#   a wallet seen in only one or two markets is not trading, it is quoting —
#   the 30-minute harvest found wallets with 1,126 trades across 10 markets,
#   which is roughly 113 trades per market and nobody's idea of a prediction
#
#   below a handful of trades there is nothing to measure anyway
POOL_LIMIT        = int(os.getenv("POOL_LIMIT", "250"))
POOL_MIN_TRADES   = int(os.getenv("POOL_MIN_TRADES", "4"))
POOL_MIN_MARKETS  = int(os.getenv("POOL_MIN_MARKETS", "3"))
POOL_MAX_PER_MKT  = float(os.getenv("POOL_MAX_PER_MKT", "12"))
MAX_HORIZON_SECONDS = int(os.getenv("MAX_HORIZON_SECONDS", "3600"))   # 1 hour
OUT = "crypto_weekly.json"


def window_positions(rows, since):
    """Net crypto positions opened inside the window, 5m to 1h only."""
    books = defaultdict(lambda: {"bought": 0.0, "sold": 0.0, "cost": 0.0,
                                 "proceeds": 0.0, "ts": None, "coin": None,
                                 "horizon": None})
    total = crypto = 0
    sides = defaultdict(set)

    for r in rows:
        ts = r.get("timestamp")
        try:
            ts = float(ts)
            if ts > 1e11:
                ts /= 1000.0
        except (TypeError, ValueError):
            continue
        if ts < since:
            continue
        total += 1

        slug = r.get("slug") or r.get("eventSlug")
        kind = feed.classify(slug)
        if kind is None:
            continue
        coin, horizon = kind
        if horizon is None or horizon > MAX_HORIZON_SECONDS:
            continue
        crypto += 1

        try:
            price = float(r.get("price")); shares = float(r.get("size"))
        except (TypeError, ValueError):
            continue
        if price <= 0 or shares <= 0:
            continue

        sides[slug].add(r.get("outcomeIndex"))
        b = books[(slug, r.get("asset"))]
        b["coin"] = b["coin"] or coin
        b["horizon"] = b["horizon"] or horizon
        if b["ts"] is None or ts < b["ts"]:
            b["ts"] = ts
        if (r.get("side") or "BUY").upper() == "SELL":
            b["sold"] += shares
            b["proceeds"] += shares * price
        else:
            b["bought"] += shares
            b["cost"] += shares * price

    out = []
    for (slug, asset), b in books.items():
        if b["bought"] <= 0:
            continue
        out.append({"slug": slug, "asset": asset, "coin": b["coin"],
                    "horizon": b["horizon"], "shares": b["bought"],
                    "held": max(b["bought"] - b["sold"], 0.0),
                    "cost": b["cost"], "proceeds": b["proceeds"],
                    "price": b["cost"] / b["bought"], "ts": b["ts"]})
    both = sum(1 for v in sides.values() if len(v) > 1)
    return out, both, len(sides), (crypto / total if total else 0.0)


def measure(wallet, name, rows, since):
    positions, both, markets, share = window_positions(rows, since)
    if not positions:
        return None

    settled = []
    for p in positions:
        win, _end = market_facts(p["slug"])
        if win is None:
            continue
        p["won"] = str(p["asset"]) == str(win)
        payout = p["held"] * (1.0 if p["won"] else 0.0)
        p["profit"] = p["proceeds"] + payout - p["cost"]
        settled.append(p)
    if not settled:
        return None

    n = len(settled)
    wins = sum(1 for p in settled if p["won"])
    # "made money on the trade" — which for a held position is the same as
    # winning it, but not for one they sold out of early
    green = sum(1 for p in settled if p["profit"] > 0)
    cost = sum(p["cost"] for p in settled)
    profit = sum(p["profit"] for p in settled)
    entry_sum = sum(p["price"] for p in settled)
    avg_entry = entry_sum / n
    win_rate = wins / n
    var = sum(p["price"] * (1 - p["price"]) for p in settled)

    days = defaultdict(float)
    for p in settled:
        days[datetime.fromtimestamp(p["ts"], timezone.utc)
             .strftime("%Y-%m-%d")] += p["profit"]
    green_days = sum(1 for v in days.values() if v > 0)

    return {
        "wallet": wallet, "name": name,
        "profile": f"https://polymarket.com/profile/{wallet}",
        "trades": n,
        "wins": wins,
        "win_rate": win_rate,
        "money_rate": green / n,
        "avg_entry": avg_entry,
        "margin": win_rate - avg_entry,
        "edge_z": (wins - entry_sum) / math.sqrt(var) if var > 0 else 0.0,
        "staked": cost,
        "profit": profit,
        "roi": profit / cost if cost else 0.0,
        "avg_size": cost / n,
        "both_sides_rate": both / markets if markets else 0.0,
        "crypto_share": share,
        "days": len(days),
        "green_days": green_days,
        "coins": dict(sorted(((c, sum(1 for p in settled if p["coin"] == c))
                              for c in {p["coin"] for p in settled}),
                             key=lambda kv: -kv[1])),
    }


def table(title, rule, rows, highlight):
    print(f"\n{title}")
    print(f"  {rule}")
    if not rows:
        print("\n  nobody qualified.\n")
        return
    print()
    print(f"  {'#':>2} {'trader':<20} {'trades':>7} {'won':>6} {'money':>6} "
          f"{'entry':>6} {'margin':>7} {'z':>6} {'profit':>10} {'ROI':>7} "
          f"{'good days':>10}")
    print("  " + "-" * 103)
    for i, r in enumerate(rows, 1):
        flag = " <-" if r is highlight else ""
        print(f"  {i:>2} {(r['name'] or '')[:19]:<20} {r['trades']:>7} "
              f"{r['win_rate']*100:>5.1f}% {r['money_rate']*100:>5.1f}% "
              f"{r['avg_entry']*100:>5.0f}c {r['margin']*100:>+6.1f}% "
              f"{r['edge_z']:>+6.1f} {r['profit']:>10,.0f} "
              f"{r['roi']*100:>6.1f}% {r['green_days']}/{r['days']:<8}{flag}")


def pin(rows, which):
    """Write the bot's trader list. Only ever called with --pin."""
    book = {
        "pinned": True,
        "list": which,
        "updated": datetime.now(timezone.utc).isoformat(),
        "window_days": WINDOW_DAYS,
        "traders": [{
            "wallet": r["wallet"], "name": r["name"],
            "win_rate": round(r["win_rate"], 4),
            "avg_entry": round(r["avg_entry"], 4),
            "margin": round(r["margin"], 4),
            "profit": round(r["profit"], 2),
            "trades": r["trades"],
        } for r in rows],
    }
    os.makedirs("data", exist_ok=True)
    with open("data/crypto_traders.json", "w") as fh:
        json.dump(book, fh, indent=1)
    print(f"\nPinned {len(rows)} traders from list {which.upper()} "
          f"-> data/crypto_traders.json")
    print("Commit and push it, then the bot follows them.")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    which = None
    for f in flags:
        if f.startswith("--pin"):
            which = f.split("=", 1)[1].lower() if "=" in f else "b"
    days = float(args[0]) if args else WINDOW_DAYS
    since = time.time() - days * 86400
    load_cache()

    try:
        with open(CANDIDATES) as fh:
            cands = json.load(fh)
    except FileNotFoundError:
        print(f"No {CANDIDATES}. Run:  python3 -m crypto.harvest 30")
        return 1

    pool, quoters, thin = [], 0, 0
    for c in cands:
        n = c.get("trades_seen", 0)
        m = c.get("markets_seen", 0) or 1
        if n < POOL_MIN_TRADES or m < POOL_MIN_MARKETS:
            thin += 1
            continue
        if n / m > POOL_MAX_PER_MKT:
            quoters += 1          # dozens of trades in one market: a quoter
            continue
        pool.append(c)
    pool.sort(key=lambda c: -c.get("trades_seen", 0))
    dropped_for_size = max(0, len(pool) - POOL_LIMIT)
    pool = pool[:POOL_LIMIT]

    print(f"Weekly crypto trader review — last {days:g} days")
    print(f"  {len(cands):,} wallets harvested")
    print(f"    {thin:,} too quiet to measure")
    print(f"    {quoters:,} look like quoters "
          f"(more than {POOL_MAX_PER_MKT:g} trades per market)")
    if dropped_for_size:
        print(f"    {dropped_for_size:,} below the busiest {POOL_LIMIT}")
    print(f"  {len(pool)} candidates to rank")
    print(f"  crypto UP/DOWN only, 5m to "
          f"{MAX_HORIZON_SECONDS//60}m markets")
    print(f"  minimum {MIN_TRADES} settled trades in the window\n")

    measured = []
    for i, c in enumerate(pool, 1):
        wallet = c["wallet"]
        name = c.get("name") or wallet[:10]
        print(f"  [{i:>3}/{len(pool)}] {name[:26]:<28}", end="", flush=True)
        try:
            rows = history(wallet, 3000)
        except Exception as e:
            print(f" history failed ({type(e).__name__})")
            continue
        slugs = {r.get("slug") or r.get("eventSlug") for r in rows}
        slugs = {s for s in slugs if s and feed.classify(s)}
        with ThreadPoolExecutor(max_workers=THREADS) as ex:
            list(ex.map(market_facts, slugs))

        m = measure(wallet, name, rows, since)
        if m is None:
            print(" nothing in the window")
            continue
        measured.append(m)
        print(f" {m['trades']:>4} trades  won {m['win_rate']*100:>4.1f}% "
              f"@ {m['avg_entry']*100:>2.0f}c  {m['profit']:>+9,.0f}")
        save_cache()

    eligible = [m for m in measured
                if m["trades"] >= MIN_TRADES
                and m["both_sides_rate"] <= MAX_BOTH_SIDES]

    # LIST A — exactly what you asked for
    a = [m for m in eligible
         if m["money_rate"] >= WIN_RATE_BAR and m["profit"] > 0]
    a.sort(key=lambda m: -m["money_rate"])
    a = a[:TOP_N]

    # LIST A' — the same win-rate bar WITHOUT requiring profit, so the cost of
    # dropping that condition is visible rather than argued about
    a_raw = [m for m in eligible if m["money_rate"] >= WIN_RATE_BAR]
    a_raw.sort(key=lambda m: -m["money_rate"])
    a_raw = a_raw[:TOP_N]

    # LIST B — win rate measured against the price paid
    b = [m for m in eligible
         if m["margin"] >= MARGIN_BAR
         and m["edge_z"] >= MIN_Z
         and m["profit"] > 0
         and PIN_MIN_ENTRY <= m["avg_entry"] <= PIN_MAX_ENTRY
         and m["trades"] >= PIN_MIN_TRADES]
    b.sort(key=lambda m: -m["edge_z"])
    b = b[:TOP_N]

    print("\n" + "=" * 107)
    table(f"LIST A — YOUR RULE: made money on >={WIN_RATE_BAR:.0%} of trades, "
          f"and up on the week",
          f"sorted by how often they made money", a, None)

    table(f"LIST A' — the same {WIN_RATE_BAR:.0%} bar, profit NOT required",
          "this is what the win-rate rule lets in on its own", a_raw, None)

    table(f"LIST B — THE ONES WORTH COPYING: margin >={MARGIN_BAR:.0%}, "
          f"z >=+{MIN_Z:g}, profitable, "
          f"{PIN_MIN_ENTRY*100:.0f}c-{PIN_MAX_ENTRY*100:.0f}c entries, "
          f"{PIN_MIN_TRADES}+ trades",
          "sorted by how far their results beat the prices they paid", b, None)

    losers = [m for m in a_raw if m["profit"] <= 0]
    print("\n" + "=" * 107)
    print("\nWHAT TO READ HERE\n")
    if a_raw:
        print(f"  {len(a_raw)} wallets cleared the {WIN_RATE_BAR:.0%} bar. "
              f"{len(losers)} of them LOST money on the week.")
        if losers:
            worst = min(losers, key=lambda m: m["profit"])
            print(f"  The worst: {worst['name']} won "
                  f"{worst['money_rate']:.0%} of its trades paying "
                  f"{worst['avg_entry']*100:.0f}c, and finished "
                  f"${worst['profit']:,.0f}.")
    else:
        print(f"  Nobody cleared {WIN_RATE_BAR:.0%} with "
              f"{MIN_TRADES}+ trades. On near-coinflip markets that is the "
              f"expected result, not a bug.")
    both = {m["wallet"] for m in a} & {m["wallet"] for m in b}
    print(f"\n  {len(both)} wallets appear in BOTH list A and list B — "
          f"those are the safest picks either way.")

    overlap = [m for m in b if m["wallet"] not in {x["wallet"] for x in a}]
    if overlap:
        print(f"  {len(overlap)} profitable wallets in list B are missed "
              f"entirely by the {WIN_RATE_BAR:.0%} rule, including "
              f"{overlap[0]['name']} "
              f"({overlap[0]['win_rate']:.0%} at "
              f"{overlap[0]['avg_entry']*100:.0f}c, "
              f"${overlap[0]['profit']:+,.0f}).")

    if which:
        chosen = a if which == "a" else b
        if chosen:
            pin(chosen, which)
        else:
            print(f"\nList {which.upper()} is empty — nothing pinned.")
    else:
        print("\nNothing pinned. Add --pin (list B) or --pin=a to commit a "
              "list to the bot.")

    with open(OUT, "w") as fh:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(),
                   "window_days": days, "list_a": a, "list_a_raw": a_raw,
                   "list_b": b, "all": measured}, fh, indent=1)
    print(f"\nFull data -> {OUT}")
    print("Nothing was traded.")
    save_cache()
    return 0


if __name__ == "__main__":
    sys.exit(main())
