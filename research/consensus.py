#!/usr/bin/env python3
"""
consensus.py -- does the CROWD of crypto traders know which way a market goes?

The copy bot follows individuals. Over 231 markets our six traders landed on
the same market 23 times and took OPPOSITE sides in 19 of them. That is what
noise looks like. The question this answers is different: pool hundreds of
traders, take the majority view per market, and see whether the majority is
right more often than the price it is paying.

Four guards, each there because of a specific way we have already fooled
ourselves:

  1. NO LOOKAHEAD. A vote only counts if it was cast before our decision
     cutoff. Counting trades placed later means betting on information we
     could not have had -- it produces beautiful, fake results.

  2. BEAT THE PRICE, NOT 50%. A majority that is right 60% of the time on
     markets priced at 60c has no edge. Every number here is measured against
     what it would have cost.

  3. OUT-OF-SAMPLE SPLIT. Rules are chosen on the first half of the data and
     scored on the second half, which the chooser never saw. Picking the best
     threshold on all the data is how you get 14 "significant" wallets when
     chance gives 6.

  4. REPORT n AND t. A result with no sample size attached is a story.

Read-only. Places no orders, touches no bot state.

    python3 -m research.consensus                 # 770 wallets, 60s cutoff
    python3 -m research.consensus 400 30          # fewer wallets, 30s cutoff
"""
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from crypto import api, feed

CANDIDATES = "crypto_candidates.json"
OUT = "research/consensus_result.json"

MIN_TRADES = 5          # wallet must have been seen this often in the harvest
MIN_MARKETS = 3
DECISION_LAG = 60.0     # seconds after market open that we stop counting votes


def load_wallets(limit):
    rows = json.load(open(CANDIDATES))
    rows = [w for w in rows
            if w.get("trades_seen", 0) >= MIN_TRADES
            and w.get("markets_seen", 0) >= MIN_MARKETS]
    rows.sort(key=lambda w: -w.get("trades_seen", 0))
    return rows[:limit]


def market_open_ts(slug):
    """Crypto slugs end in the window's unix timestamp."""
    tail = slug.rsplit("-", 1)[-1]
    return float(tail) if tail.isdigit() else None


def collect_votes(wallets):
    """Every crypto UP/DOWN buy each wallet made, keyed by market."""
    votes = defaultdict(list)
    for i, w in enumerate(wallets):
        if i % 25 == 0:
            print(f"  wallet {i}/{len(wallets)}  markets so far {len(votes)}")
        offset = 0
        while offset < 1000:
            acts = api.activity(w["wallet"], limit=500, offset=offset)
            if not acts:
                break
            for a in acts:
                if (a.get("type") or a.get("side", "")).upper() not in ("TRADE", "BUY"):
                    continue
                if str(a.get("side", "")).upper() == "SELL":
                    continue
                slug = a.get("slug") or ""
                if not feed.CRYPTO_SLUG.match(slug):
                    continue
                ts = a.get("timestamp") or a.get("time")
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    continue
                try:
                    price = float(a.get("price"))
                except (TypeError, ValueError):
                    continue
                votes[slug].append({
                    "wallet": w["wallet"],
                    "outcome": a.get("outcome"),
                    "price": price,
                    "ts": ts,
                    "usd": float(a.get("usdcSize") or a.get("size") or 0),
                })
            if len(acts) < 500:
                break
            offset += 500
            time.sleep(0.05)
        time.sleep(0.05)
    return votes


def resolve(slugs):
    """Which side won, per market."""
    out = {}
    for i, slug in enumerate(slugs):
        if i % 100 == 0:
            print(f"  resolving {i}/{len(slugs)}")
        m = api._get(f"{api.GAMMA_API}/markets", {"slug": slug, "closed": "true"})
        rows = api._rows(m)
        if not rows:
            continue
        mk = rows[0]
        prices = api._maybe_json(mk.get("outcomePrices"))
        outcomes = api._maybe_json(mk.get("outcomes"))
        if not prices or not outcomes:
            continue
        try:
            p = [float(x) for x in prices]
        except (TypeError, ValueError):
            continue
        if abs(sum(p) - 1.0) > 0.01 or max(p) < 0.99:
            continue
        out[slug] = outcomes[p.index(max(p))]
        time.sleep(0.03)
    return out


def score(votes, winners, min_voters, margin_bar, min_disagree=None):
    """min_disagree: only bet when the crowd's confidence exceeds the price
    by this much. None = ignore the price and bet on the vote alone."""
    """One row per market we would actually have bet."""
    rows = []
    for slug, vs in votes.items():
        w = winners.get(slug)
        if not w:
            continue
        open_ts = market_open_ts(slug)
        if open_ts is None:
            continue
        # GUARD 1: only votes cast before we would have had to act
        early = [v for v in vs if v["ts"] <= open_ts + DECISION_LAG]
        if len(early) < min_voters:
            continue
        tally = defaultdict(int)
        paid = defaultdict(list)
        for v in early:
            tally[v["outcome"]] += 1
            paid[v["outcome"]].append(v["price"])
        side, n_side = max(tally.items(), key=lambda kv: kv[1])
        total = sum(tally.values())
        share = n_side / total
        if share < margin_bar:
            continue
        price = sum(paid[side]) / len(paid[side])
        # THE REAL SIGNAL: the crowd says `share`, the market charges `price`.
        # Betting when they agree pays nothing -- the gap is the whole idea.
        disagree = share - price
        if min_disagree is not None and disagree < min_disagree:
            continue
        won = 1 if side == w else 0
        rows.append({"slug": slug, "side": side, "voters": total,
                     "share": round(share, 3), "price": round(price, 4),
                     "disagree": round(disagree, 3),
                     "won": won, "open_ts": open_ts,
                     "pnl": (1 - price) if won else -price})
    return rows


def summarise(rows, label):
    if not rows:
        print(f"{label:<28} no markets")
        return None
    n = len(rows)
    wins = sum(r["won"] for r in rows)
    paid = sum(r["price"] for r in rows)
    pnl = [r["pnl"] for r in rows]
    mean = sum(pnl) / n
    sd = (sum((x - mean) ** 2 for x in pnl) / (n - 1)) ** 0.5 if n > 1 else 0
    t = mean / (sd / math.sqrt(n)) if sd else 0
    # GUARD 2: the bar is the price paid, not 50%
    implied = paid / n
    print(f"{label:<28}{n:>6}{wins/n*100:>9.1f}%{implied*100:>10.1f}%"
          f"{(wins/n - implied)*100:>+9.1f}pp{mean*100:>+9.2f}c{t:>8.2f}")
    return {"label": label, "n": n, "win_rate": wins/n, "implied": implied,
            "edge": wins/n - implied, "per_trade": mean, "t": t}


def run(limit, lag):
    global DECISION_LAG
    DECISION_LAG = lag
    wallets = load_wallets(limit)
    print(f"{len(wallets)} wallets, votes counted up to {lag:.0f}s after open\n")

    print("collecting votes")
    votes = collect_votes(wallets)
    print(f"\n{len(votes)} markets had at least one vote")

    multi = [s for s, v in votes.items() if len(v) >= 3]
    print(f"{len(multi)} markets had 3+ voters -- these are the only useful ones\n")

    print("resolving outcomes")
    winners = resolve(multi)
    print(f"{len(winners)} resolved\n")

    # GUARD 3: choose on the first half, score on the second
    all_rows = score(votes, winners, 3, 0.0)
    all_rows.sort(key=lambda r: r["open_ts"])
    cut = len(all_rows) // 2
    first = {r["slug"] for r in all_rows[:cut]}

    print("=" * 74)
    print(f"{'rule':<28}{'n':>6}{'won':>9}{'priced':>10}{'edge':>9}{'per $1':>9}{'t':>8}")
    print("=" * 74)

    results = []
    combos = [(v, b, None) for v in (3, 5, 10) for b in (0.60, 0.70, 0.80)]
    combos += [(v, 0.60, d) for v in (5, 10) for d in (0.05, 0.10, 0.20)]
    for voters, bar, dis in combos:
            rows = score(votes, winners, voters, bar, dis)
            a = [r for r in rows if r["slug"] in first]
            b = [r for r in rows if r["slug"] not in first]
            tag = (f"{voters}+ voters, {bar*100:.0f}% agree"
                   + (f", crowd beats price by {dis*100:.0f}pp" if dis else ""))
            r1 = summarise(a, f"  IN-SAMPLE  {tag}")
            r2 = summarise(b, f"  OUT-SAMPLE {tag}")
            if r1 and r2:
                results.append({"rule": tag, "in": r1, "out": r2})
    print("-" * 74)

    print("\nRead the OUT-SAMPLE lines only. The in-sample ones are there to show")
    print("how much better things look when you pick the rule on the same data.")
    print("A rule is worth anything only if OUT-SAMPLE edge is positive and t > 2.")

    os.makedirs("research", exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(),
                   "wallets": len(wallets), "decision_lag": lag,
                   "markets_scored": len(all_rows),
                   "results": results, "rows": all_rows[:5000]}, f, indent=2)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 770,
        float(sys.argv[2]) if len(sys.argv) > 2 else 60.0)
