#!/usr/bin/env python3
"""
rank_traders.py — who is actually worth copying, in SHORT markets.

The leaderboard ranks by profit over a window. That is how SDTrading got
copied: a good week on the board, minus $397,834 lifetime, and it wiped the
backtest account in 45 trades.

This looks at what each wallet actually does, from their own trade history:

    short trades    how many of their trades are in markets that settle
                    within MAX_MARKET_DAYS, which is all your bot copies
    win rate        of those, how many resolved in their favour
    avg entry       the average price they pay. This is the column that
                    decides whether a high win rate is worth anything: at
                    92c you need to win 92% just to break even
    profit          what those short trades made, held to resolution
    per trade       profit divided by trades. Separates a real operator from
                    a high-frequency grinder scraping pennies

Read-only. No wallet, no orders, nothing written except a cache.

    python3 rank_traders.py              # 500 trades per wallet
    python3 rank_traders.py 1500         # deeper, slower
    MAX_MARKET_DAYS=1 python3 rank_traders.py     # only same-day markets
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from bot import config
from bot import polymarket as pm

GAMMA = "https://gamma-api.polymarket.com"
CACHE_FILE = ".market_cache_v2.json"
THREADS = 24

_cache = {}


# ----------------------------------------------------------------- caching
def load_cache():
    if not os.path.exists(CACHE_FILE):
        return
    try:
        raw = json.load(open(CACHE_FILE))
        for k, v in raw.items():
            _cache[k] = tuple(v)
    except Exception:
        pass


def save_cache():
    try:
        json.dump({k: list(v) for k, v in _cache.items()}, open(CACHE_FILE, "w"))
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
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def market_facts(token_id, slug):
    """(won, end_ts) for a token — won is True/False/None."""
    if token_id in _cache:
        e = _cache[token_id]
        return e[0], e[1]

    won, end = None, None
    if slug:
        rows = []
        for params in ({"slug": slug, "limit": 1, "closed": "true"},
                       {"slug": slug, "limit": 1}):
            rows = pm._rows(pm._get(f"{GAMMA}/markets", params, tries=1))
            if rows:
                break
        if rows:
            m = rows[0]
            end = (_ts(m.get("endDate")) or _ts(m.get("end_date_iso"))
                   or _ts(m.get("closedTime")))
            if m.get("closed") or m.get("resolved"):
                prices = m.get("outcomePrices")
                ids = m.get("clobTokenIds")
                if isinstance(prices, str):
                    try: prices = json.loads(prices)
                    except Exception: prices = None
                if isinstance(ids, str):
                    try: ids = json.loads(ids)
                    except Exception: ids = None
                if prices:
                    idx = ids.index(token_id) if isinstance(ids, list) and token_id in ids else 0
                    try:
                        won = float(prices[idx]) >= 0.5
                    except (ValueError, IndexError, TypeError):
                        won = None

    _cache[token_id] = (won, end, slug)
    return won, end


# ------------------------------------------------------------------ history
def history(wallet, limit):
    rows, offset, page = [], 0, 500
    while len(rows) < limit:
        batch = pm._rows(pm._get(f"{config.DATA_API}/activity",
                                 {"user": wallet, "limit": min(page, limit - len(rows)),
                                  "offset": offset, "type": "TRADE"}))
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += len(batch)
        time.sleep(0.15)
    return rows


def main():
    per_wallet = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    max_days = config.MAX_MARKET_DAYS

    print("Ranking traders by what they do in SHORT markets\n")
    print(f"  short = settles within {max_days:g} days of the trade")
    print(f"  {per_wallet} trades examined per wallet\n")

    # widen the pool: we want to rank everyone, not just who passes the filter
    rows = pm._rows(pm._get(f"{config.DATA_API}/v1/leaderboard",
                            {"period": "7d", "limit": 100}))
    wallets, seen = [], set()
    for r in rows:
        w = r.get("proxyWallet") or r.get("wallet")
        if not w or w.lower() in seen:
            continue
        seen.add(w.lower())
        wallets.append({"wallet": w,
                        "name": r.get("userName") or r.get("name") or w[:12],
                        "board_pnl": float(r.get("pnl") or 0)})
    if not wallets:
        print("leaderboard unreachable")
        return 1
    print(f"{len(wallets)} wallets on the leaderboard\n")

    # pull everyone's history first
    trades = {}
    for i, w in enumerate(wallets, 1):
        h = history(w["wallet"], per_wallet)
        trades[w["wallet"]] = h
        print(f"  [{i:>3}/{len(wallets)}] {w['name'][:22]:22} {len(h):>5} trades")
        time.sleep(0.1)

    # look every market up once, in parallel
    load_cache()
    pending = {}
    for h in trades.values():
        for t in h:
            tok = t.get("asset")
            if tok and tok not in _cache:
                pending.setdefault(tok, t.get("slug"))
    if pending:
        print(f"\n  looking up {len(pending)} markets ({THREADS} at a time)...")
        done = [0]
        def one(item):
            try:
                market_facts(*item)
            except Exception:
                _cache[item[0]] = (None, None, item[1])
            done[0] += 1
            if done[0] % 500 == 0:
                print(f"    {done[0]}/{len(pending)}", flush=True)
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            list(pool.map(one, pending.items()))
        save_cache()
    print()

    # score each wallet on short markets only
    results = []
    for w in wallets:
        n = wins = 0
        spend = 0.0
        payout = 0.0
        entry_sum = 0.0
        long_n = 0

        for t in trades[w["wallet"]]:
            if (t.get("side") or "").upper() != "BUY":
                continue
            price = float(t.get("price") or 0)
            usd = float(t.get("usdcSize") or t.get("size") or 0)
            if price <= 0 or usd <= 0:
                continue
            tok = t.get("asset")
            traded_at = float(t.get("timestamp") or 0)
            won, end = market_facts(tok, t.get("slug"))
            if end is None or won is None:
                continue
            days = (end - traded_at) / 86400
            if days > max_days:
                long_n += 1
                continue
            if days < 0:
                continue

            n += 1
            spend += usd
            entry_sum += price
            if won:
                wins += 1
                payout += usd / price        # each share pays $1

        if n < 10:                            # too few to say anything
            continue

        profit = payout - spend
        results.append({
            "name": w["name"],
            "wallet": w["wallet"],
            "board_pnl": w["board_pnl"],
            "n": n,
            "win_rate": wins / n * 100,
            "avg_entry": entry_sum / n,
            "profit": profit,
            "per_trade": profit / n,
            "long_n": long_n,
        })

    if not results:
        print("No wallet had enough short-market trades to judge.")
        print("Try a wider window: MAX_MARKET_DAYS=7 python3 rank_traders.py")
        return 0

    def table(title, key, rows_):
        print(f"\n{title}\n")
        print(f"{'trader':22}{'trades':>7}{'win%':>7}{'avg entry':>11}"
              f"{'profit':>12}{'per trade':>11}{'board pnl':>12}")
        print("-" * 82)
        for r in sorted(rows_, key=lambda x: x[key], reverse=True)[:20]:
            print(f"{r['name'][:21]:22}{r['n']:>7}{r['win_rate']:>6.1f}%"
                  f"{r['avg_entry']*100:>10.0f}c{r['profit']:>12,.0f}"
                  f"{r['per_trade']:>11,.2f}{r['board_pnl']:>12,.0f}")

    table("BEST IN SHORT MARKETS — by profit", "profit", results)
    table("BY PROFIT PER TRADE", "per_trade", results)
    table("BY WIN RATE", "win_rate", results)

    print(f"""
Read the avg entry column against the win rate. A trader winning 85% at 90c
is losing money; one winning 55% at 40c is making it. The board pnl column is
what the leaderboard shows — where it disagrees with the profit column, the
leaderboard is measuring something your bot does not copy.

Held-to-resolution is assumed. Traders who sell early will look different in
reality, and this is their history, not a promise about their future.
""")

    json.dump(results, open("trader_ranking.json", "w"), indent=2)
    print(f"Full data -> trader_ranking.json ({len(results)} wallets)")

    if "--pin" in sys.argv:
        pin(results)
    else:
        print("\nRun again with --pin to make the bot follow these.")
    return 0


MIN_TRADES_TO_PIN = 50


def pin(results, top_n=None):
    """
    Write the winners to data/traders.json for the bot to follow.

    Ranked by profit, not win rate — Roadto1mlesgooo wins 96.5% of the time
    at 92c and is down $49,994. Wallets with fewer than MIN_TRADES_TO_PIN
    short trades are left out however good they look: 10 trades is luck.
    Unprofitable wallets are left out entirely, so the list can come back
    shorter than you asked for. Fifteen that make money beats twenty padded
    with four that don't.
    """
    top_n = top_n or config.TOP_N_TRADERS
    good = [r for r in results
            if r["n"] >= MIN_TRADES_TO_PIN and r["profit"] > 0]
    good.sort(key=lambda r: r["profit"], reverse=True)
    chosen = good[:top_n]

    if not chosen:
        print("\nNothing qualified — leaving the current list alone.")
        return

    # keep whoever the analyzer benched benched: our own measured results
    # beat their history
    existing = {}
    if os.path.exists("data/traders.json"):
        try:
            existing = json.load(open("data/traders.json"))
        except Exception:
            existing = {}
    benched = existing.get("benched", [])

    from datetime import datetime, timezone
    book = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "pinned": True,
        "ranked_on": {"max_market_days": config.MAX_MARKET_DAYS,
                      "min_trades": MIN_TRADES_TO_PIN},
        "benched": benched,
        "traders": [{"wallet": r["wallet"], "name": r["name"],
                     "pnl": round(r["profit"], 2), "volume": r["n"],
                     "win_rate": round(r["win_rate"], 1),
                     "avg_entry": round(r["avg_entry"], 3)} for r in chosen],
    }
    os.makedirs("data", exist_ok=True)
    json.dump(book, open("data/traders.json", "w"), indent=2)

    print(f"\nPinned {len(chosen)} traders -> data/traders.json")
    print(f"{'trader':22}{'trades':>8}{'win%':>7}{'entry':>8}{'profit':>12}")
    print("-" * 57)
    for r in chosen:
        print(f"{r['name'][:21]:22}{r['n']:>8}{r['win_rate']:>6.1f}%"
              f"{r['avg_entry']*100:>7.0f}c{r['profit']:>12,.0f}")
    if benched:
        print(f"\n{len(benched)} still benched from your own results.")
    print("\nCommit and push data/traders.json — the bot picks it up next pass.")


if __name__ == "__main__":
    raise SystemExit(main())
