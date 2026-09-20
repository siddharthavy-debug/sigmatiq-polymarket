#!/usr/bin/env python3
"""
rank_history.py — which crypto wallets are actually worth copying.

Polymarket shows you total profit. Total profit is the wrong question twice
over: it is dominated by sports and politics, and it rewards size rather than
skill. This answers a narrower one — on short-duration crypto UP/DOWN markets,
did this wallet beat the price it paid, often enough and steadily enough to be
worth following?

THE CENTRAL NUMBER is not the win rate. It is the win rate measured against
the entry price. A wallet paying 78c should win 78% of the time by chance
alone; winning 80% is barely an edge. A wallet paying 41c and winning 55% is
printing money. So the headline statistic here is:

    edge z-score = (wins - sum of entry prices) / sqrt(sum of p(1-p))

Expected wins, if the market is priced fairly, is just the sum of the prices
paid. Beating that by more than about 2 standard deviations is hard to do by
luck. This is the same arithmetic that flagged Roadto1mlesgooo — 96.5% win
rate, minus fifty thousand dollars.

Read-only: no wallet key, no orders, nothing written but a cache and a report.

    python3 -m crypto.rank_history                 # uses crypto_candidates.json
    python3 -m crypto.rank_history 3000            # deeper history per wallet
    MIN_POSITIONS=40 python3 -m crypto.rank_history # looser, for a first look
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

GAMMA = pm.GAMMA_API
CACHE_FILE = ".crypto_market_cache.json"
CANDIDATES = "crypto_candidates.json"
OUT_JSON = "crypto_ranking.json"
THREADS = 24

# ---- gates: a candidate must clear all of these to be ranked at all --------
MIN_POSITIONS   = int(os.getenv("MIN_POSITIONS", "80"))    # sample size
MIN_CRYPTO_SHARE = float(os.getenv("MIN_CRYPTO_SHARE", "0.60"))
MAX_BOTH_SIDES   = float(os.getenv("MAX_BOTH_SIDES", "0.15"))  # market maker
MIN_EDGE_MARGIN  = float(os.getenv("MIN_EDGE_MARGIN", "0.03"))  # win% over price
MIN_EDGE_Z       = float(os.getenv("MIN_EDGE_Z", "2.0"))   # beat the price by
                                                           # more than luck
ACTIVE_WITHIN_DAYS = float(os.getenv("ACTIVE_WITHIN_DAYS", "4"))

_cache = {}


# ------------------------------------------------------------------- cache
def load_cache():
    try:
        with open(CACHE_FILE) as fh:
            _cache.update({k: tuple(v) for k, v in json.load(fh).items()})
    except Exception:
        pass


def save_cache():
    try:
        with open(CACHE_FILE, "w") as fh:
            json.dump({k: list(v) for k, v in _cache.items()}, fh)
    except Exception:
        pass


def _ts(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def market_facts(slug):
    """(winning_token_id, end_ts). Either may be None."""
    if slug in _cache:
        return _cache[slug]

    win, end = None, None
    rows = []
    # settled markets are hidden from Gamma's default query, and everything
    # here is settled, so closed=true goes first
    for params in ({"slug": slug, "limit": 1, "closed": "true"},
                   {"slug": slug, "limit": 1}):
        rows = pm._rows(pm._get(f"{GAMMA}/markets", params, tries=1))
        if rows:
            break
    if rows:
        m = rows[0]
        end = _ts(m.get("endDate")) or _ts(m.get("closedTime"))
        prices, ids = m.get("outcomePrices"), m.get("clobTokenIds")
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except Exception: prices = None
        if isinstance(ids, str):
            try: ids = json.loads(ids)
            except Exception: ids = None
        # the winning token is the one whose price settled at 1, matched by
        # index against clobTokenIds. Assuming [Yes, No] order instead would
        # turn every win into a loss.
        if prices and ids and len(prices) == len(ids):
            for i, p in enumerate(prices):
                try:
                    if float(p) >= 0.99:
                        win = str(ids[i])
                        break
                except (TypeError, ValueError):
                    continue
    _cache[slug] = (win, end)
    return win, end


# ----------------------------------------------------------------- history
def history(wallet, limit):
    rows, offset, page = [], 0, 500
    while len(rows) < limit:
        batch = pm._rows(pm._get(
            f"{pm.DATA_API}/activity",
            {"user": wallet, "limit": min(page, limit - len(rows)),
             "offset": offset, "type": "TRADE"}))
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += len(batch)
        time.sleep(0.12)
    return rows


def crypto_positions(rows):
    """
    Collapse a wallet's raw trades into net positions in crypto markets.

    Returns (positions, crypto_share, both_sides_markets, markets).
    Selling before resolution is handled properly — proceeds count, and only
    the remainder rides on the outcome.
    """
    books = defaultdict(lambda: {
        "bought": 0.0, "sold": 0.0, "cost": 0.0, "proceeds": 0.0,
        "ts": None, "coin": None, "horizon": None, "outcome_index": None})
    total = 0
    crypto = 0
    sides = defaultdict(set)

    for r in rows:
        total += 1
        slug = r.get("slug") or r.get("eventSlug")
        kind = feed.classify(slug)
        if kind is None:
            continue
        crypto += 1
        coin, horizon = kind
        asset = r.get("asset")
        try:
            price = float(r.get("price"))
            shares = float(r.get("size"))
        except (TypeError, ValueError):
            continue
        if price <= 0 or shares <= 0:
            continue
        ts = _ts(r.get("timestamp")) or 0.0
        if ts > 1e11:
            ts /= 1000.0

        sides[slug].add(r.get("outcomeIndex"))
        b = books[(slug, asset)]
        b["coin"] = b["coin"] or coin
        b["horizon"] = b["horizon"] or horizon
        b["outcome_index"] = r.get("outcomeIndex")
        if b["ts"] is None or (ts and ts < b["ts"]):
            b["ts"] = ts
        if (r.get("side") or "BUY").upper() == "SELL":
            b["sold"] += shares
            b["proceeds"] += shares * price
        else:
            b["bought"] += shares
            b["cost"] += shares * price

    positions = []
    for (slug, asset), b in books.items():
        if b["bought"] <= 0:
            continue
        positions.append({
            "slug": slug, "asset": asset, "coin": b["coin"],
            "horizon": b["horizon"],
            "shares": b["bought"], "held": max(b["bought"] - b["sold"], 0.0),
            "cost": b["cost"], "proceeds": b["proceeds"],
            "price": b["cost"] / b["bought"], "ts": b["ts"] or 0.0,
        })

    both = sum(1 for s in sides.values() if len(s) > 1)
    share = crypto / total if total else 0.0
    return positions, share, both, len(sides)


# ----------------------------------------------------------------- scoring
def evaluate(wallet, name, rows, now):
    positions, crypto_share, both_sides, markets = crypto_positions(rows)
    if not positions:
        return None

    settled = []
    for p in positions:
        win, end = market_facts(p["slug"])
        if win is None:
            continue
        won = str(p["asset"]) == str(win)
        payout = p["held"] * (1.0 if won else 0.0)
        p["won"] = won
        p["profit"] = p["proceeds"] + payout - p["cost"]
        p["end"] = end
        p["duration"] = (end - p["ts"]) if (end and p["ts"]) else None
        settled.append(p)

    if not settled:
        return None

    n = len(settled)
    wins = sum(1 for p in settled if p["won"])
    cost = sum(p["cost"] for p in settled)
    profit = sum(p["profit"] for p in settled)
    entry_sum = sum(p["price"] for p in settled)
    avg_entry = entry_sum / n
    win_rate = wins / n

    # Did they beat the price they paid? Expected wins under fair pricing is
    # the sum of the prices. Anything else is comparing to the wrong baseline.
    var = sum(p["price"] * (1 - p["price"]) for p in settled)
    edge_z = (wins - entry_sum) / math.sqrt(var) if var > 0 else 0.0

    # day buckets -> consistency and recent form
    days = defaultdict(lambda: {"n": 0, "profit": 0.0, "cost": 0.0})
    for p in settled:
        if not p["ts"]:
            continue
        key = datetime.fromtimestamp(p["ts"], timezone.utc).strftime("%Y-%m-%d")
        d = days[key]
        d["n"] += 1
        d["profit"] += p["profit"]
        d["cost"] += p["cost"]

    day_profits = [d["profit"] for d in days.values()]
    profitable_days = sum(1 for v in day_profits if v > 0)
    day_pct = profitable_days / len(day_profits) if day_profits else 0.0
    if len(day_profits) > 1:
        mean = sum(day_profits) / len(day_profits)
        sd = math.sqrt(sum((v - mean) ** 2 for v in day_profits)
                       / (len(day_profits) - 1))
        steadiness = mean / sd if sd > 0 else 0.0
    else:
        steadiness = 0.0

    def window(days_back):
        cut = now - days_back * 86400
        sel = [p for p in settled if p["ts"] and p["ts"] >= cut]
        c = sum(p["cost"] for p in sel)
        pr = sum(p["profit"] for p in sel)
        w = sum(1 for p in sel if p["won"])
        return {"n": len(sel), "profit": round(pr, 2),
                "roi": round(pr / c, 4) if c else 0.0,
                "win_rate": round(w / len(sel), 4) if sel else 0.0}

    durations = [p["duration"] for p in settled if p["duration"]]
    last_ts = max((p["ts"] for p in settled if p["ts"]), default=0)

    return {
        "wallet": wallet,
        "name": name,
        "profile": f"https://polymarket.com/profile/{wallet}",
        "crypto_positions": n,
        "crypto_markets": markets,
        "crypto_share_of_activity": round(crypto_share, 3),
        "both_sides_markets": both_sides,
        "both_sides_rate": round(both_sides / markets, 3) if markets else 0.0,
        "win_rate": round(win_rate, 4),
        "avg_entry": round(avg_entry, 4),
        "edge_margin": round(win_rate - avg_entry, 4),
        "edge_z": round(edge_z, 2),
        "capital_staked": round(cost, 2),
        "crypto_profit": round(profit, 2),
        "roi": round(profit / cost, 4) if cost else 0.0,
        "avg_trade_size_usd": round(cost / n, 2),
        "avg_duration_minutes": round(sum(durations) / len(durations) / 60, 1)
                                if durations else None,
        "days_active": len(days),
        "profitable_days_pct": round(day_pct, 3),
        "steadiness": round(steadiness, 3),
        "last_trade_days_ago": round((now - last_ts) / 86400, 2) if last_ts else None,
        "coins": dict(sorted(
            ((c, sum(1 for p in settled if p["coin"] == c))
             for c in {p["coin"] for p in settled}), key=lambda kv: -kv[1])),
        "by_horizon": {
            (f"{int(h/60)}m" if h else "other"): sum(
                1 for p in settled if p["horizon"] == h)
            for h in {p["horizon"] for p in settled}},
        "w7": window(7), "w30": window(30), "w90": window(90),
    }


def gate(r):
    """Why a candidate is excluded, or None if it qualifies."""
    if r["crypto_positions"] < MIN_POSITIONS:
        return f"only {r['crypto_positions']} settled crypto trades"
    if r["crypto_share_of_activity"] < MIN_CRYPTO_SHARE:
        return f"only {r['crypto_share_of_activity']:.0%} of activity is crypto"
    if r["both_sides_rate"] > MAX_BOTH_SIDES:
        return f"both sides in {r['both_sides_rate']:.0%} of markets (market maker)"
    if r["edge_margin"] < MIN_EDGE_MARGIN:
        return f"win rate only {r['edge_margin']:+.1%} vs the price paid"
    if r["edge_z"] < MIN_EDGE_Z:
        return f"edge z={r['edge_z']:+.1f} — inside the range of luck"
    if r["roi"] <= 0:
        return f"ROI {r['roi']:+.1%}"
    if r["last_trade_days_ago"] is not None and \
            r["last_trade_days_ago"] > ACTIVE_WITHIN_DAYS:
        return f"inactive {r['last_trade_days_ago']:.0f} days"
    return None


def score(r):
    """
    Transparent and deliberately boring.

      45%  edge over the price paid, capped at z=6
      20%  ROI on capital staked, capped at 50%
      20%  consistency — share of days in profit, and steadiness of daily P&L
      15%  recent form — last 7 days weighted against the 30-day record

    Skill first, size never. A wallet cannot buy its way up this list.
    """
    edge = min(max(r["edge_z"], 0) / 6.0, 1.0)
    roi = min(max(r["roi"], 0) / 0.50, 1.0)
    consistency = 0.6 * r["profitable_days_pct"] + \
        0.4 * min(max(r["steadiness"], 0) / 1.5, 1.0)
    recent = r["w7"]["roi"]
    recency = min(max(recent, 0) / 0.50, 1.0) if r["w7"]["n"] >= 5 else 0.5
    return round(100 * (0.45 * edge + 0.20 * roi +
                        0.20 * consistency + 0.15 * recency), 1)


# -------------------------------------------------------------------- main
def main():
    per_wallet = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    now = time.time()
    load_cache()

    try:
        with open(CANDIDATES) as fh:
            cands = json.load(fh)
    except FileNotFoundError:
        print(f"No {CANDIDATES}. Run:  python3 -m crypto.harvest 30")
        return 1

    # anyone who traded crypto at least a few times in the harvest window
    pool = [c for c in cands if c.get("trades_seen", 0) >= 2]
    print("Ranking crypto UP/DOWN traders on their own settled history\n")
    print(f"  {len(pool)} candidate wallets from {CANDIDATES}")
    print(f"  up to {per_wallet} past trades examined per wallet")
    print(f"  gates: >={MIN_POSITIONS} settled crypto trades, "
          f">={MIN_CRYPTO_SHARE:.0%} crypto, "
          f"<={MAX_BOTH_SIDES:.0%} both-sided, "
          f"win rate >={MIN_EDGE_MARGIN:+.0%} over price paid, "
          f"edge z >= {MIN_EDGE_Z:+.1f}\n")

    results, rejected = [], []
    for i, c in enumerate(pool, 1):
        wallet = c["wallet"]
        name = c.get("name") or wallet[:10]
        print(f"  [{i:>3}/{len(pool)}] {name[:28]:<30}", end="", flush=True)
        try:
            rows = history(wallet, per_wallet)
        except Exception as e:
            print(f" history failed ({type(e).__name__})")
            continue
        if not rows:
            print(" no history")
            continue

        # resolve this wallet's markets in parallel; the cache means wallets
        # trading the same 5-minute markets cost almost nothing after the first
        slugs = {r.get("slug") or r.get("eventSlug") for r in rows}
        slugs = {s for s in slugs if s and feed.classify(s) and s not in _cache}
        if slugs:
            with ThreadPoolExecutor(max_workers=THREADS) as pool_x:
                list(pool_x.map(market_facts, slugs))

        r = evaluate(wallet, name, rows, now)
        if r is None:
            print(" no settled crypto trades")
            continue
        why = gate(r)
        r["score"] = score(r)
        if why:
            r["excluded"] = why
            rejected.append(r)
            print(f" {r['crypto_positions']:>4} trades — excluded: {why}")
        else:
            results.append(r)
            print(f" {r['crypto_positions']:>4} trades  "
                  f"win {r['win_rate']:.0%} @ {r['avg_entry']*100:.0f}c  "
                  f"z={r['edge_z']:+.1f}  score {r['score']}")
        save_cache()

    results.sort(key=lambda r: -r["score"])
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "source": "Polymarket data-api /activity + gamma-api /markets "
                  "(resolution), candidates from the live RTDS feed",
        "gates": {"min_positions": MIN_POSITIONS,
                  "min_crypto_share": MIN_CRYPTO_SHARE,
                  "max_both_sides": MAX_BOTH_SIDES,
                  "min_edge_margin": MIN_EDGE_MARGIN,
                  "min_edge_z": MIN_EDGE_Z,
                  "active_within_days": ACTIVE_WITHIN_DAYS},
        "qualified": results,
        "excluded": sorted(rejected, key=lambda r: -r["crypto_positions"])[:100],
    }
    with open(OUT_JSON, "w") as fh:
        json.dump(payload, fh, indent=1)

    print(f"\n\n{len(results)} QUALIFIED CANDIDATES\n")
    print(f"{'#':>3} {'name':<20} {'trades':>7} {'win%':>6} {'entry':>6} "
          f"{'edge':>6} {'z':>6} {'ROI':>7} {'profit':>10} {'days+':>6} "
          f"{'7d ROI':>7} {'score':>6}")
    print("-" * 108)
    for i, r in enumerate(results, 1):
        print(f"{i:>3} {(r['name'] or '')[:19]:<20} {r['crypto_positions']:>7} "
              f"{r['win_rate']*100:>5.1f}% {r['avg_entry']*100:>5.0f}c "
              f"{r['edge_margin']*100:>+5.1f}% {r['edge_z']:>+6.1f} "
              f"{r['roi']*100:>6.1f}% {r['crypto_profit']:>10,.0f} "
              f"{r['profitable_days_pct']*100:>5.0f}% "
              f"{r['w7']['roi']*100:>6.1f}% {r['score']:>6.1f}")

    print("\nWALLET ADDRESSES\n")
    for i, r in enumerate(results, 1):
        print(f"{i:>3} {r['wallet']}  {r['name']}")

    print(f"\nFull detail -> {OUT_JSON}")
    print("\nRead the entry column against the win rate. A wallet winning 85%")
    print("at 90c is losing money. The z column is the one that matters: it is")
    print("how far their results beat the prices they paid, in standard")
    print("deviations. Below about +2, assume luck.")
    save_cache()
    return 0


if __name__ == "__main__":
    sys.exit(main())
