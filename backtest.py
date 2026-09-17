#!/usr/bin/env python3
"""
backtest.py — would copying these traders have made money?

Replays real history through the exact rules the live bot uses, then looks up
how each market actually resolved.

    1. pull the same top-20 leaderboard the bot uses
    2. pull each trader's past trades from /activity  (price, size, timestamp)
    3. merge into one timeline, oldest first
    4. run it through handle_buy / handle_sell — same $3 size, 5% per-market
       cap, price bounds, per-trader cap, pacing, proportional sells
    5. settle whatever is still open using each market's real resolution
    6. report what $100 would have become

Read-only. No wallet, no orders, no state files touched.

    python3 backtest.py                       # 500 trades per wallet
    python3 backtest.py 2000                  # deeper history

Compare horizons without editing anything:

    MAX_MARKET_DAYS=1 python3 backtest.py 2000     # same-day markets only
    MAX_MARKET_DAYS=3 python3 backtest.py 2000
    MAX_MARKET_DAYS=7 python3 backtest.py 2000

Run the same history through each and see which window actually pays.

WHAT THIS CANNOT TELL YOU
  * It fills at THEIR price. Your copy lands seconds later at a worse one, so
    real results come out below this. Treat the number as a ceiling.
  * /activity only reaches back so far — this is recent history, not years.
  * These are the wallets on the leaderboard TODAY. Anyone who blew up isn't
    in the list, so the sample is survivors only. That flatters the result.
"""
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from bot import config, polymarket as pm

GAMMA = "https://gamma-api.polymarket.com"
CACHE_FILE = ".market_cache_v2.json"   # v1 held wrong "unresolved" answers
LOOKUP_THREADS = 24

_resolution_cache = {}


def load_cache():
    """Market facts don't change once settled, so keep them between runs.
    Makes the second and third comparison run nearly instant."""
    if not os.path.exists(CACHE_FILE):
        return
    try:
        with open(CACHE_FILE) as f:
            raw = json.load(f)
        for k, v in raw.items():
            _resolution_cache[k] = tuple(v)
            if len(v) > 2 and v[2]:
                pm._end_cache[v[2]] = v[1]
        print(f"  cache: {len(_resolution_cache)} markets already known")
    except Exception:
        pass


def save_cache():
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({k: list(v) for k, v in _resolution_cache.items()}, f)
    except Exception:
        pass


def prefetch(timeline):
    """
    Look every market up once, in parallel, before the replay starts.

    Done serially this is the whole runtime — thousands of round trips at a
    third of a second each. The replay itself takes seconds.
    """
    pairs = {}
    for row in timeline:
        tok = row.get("asset")
        if tok and tok not in _resolution_cache:
            pairs.setdefault(tok, row.get("slug"))
    if not pairs:
        return

    print(f"  looking up {len(pairs)} markets ({LOOKUP_THREADS} at a time)...")
    done = [0]
    total = len(pairs)

    def one(item):
        tok, slug = item
        try:
            resolution_info(tok, slug)
        except Exception:
            _resolution_cache[tok] = (None, None)
        done[0] += 1
        if done[0] % 250 == 0:
            print(f"    {done[0]}/{total}", flush=True)

    with ThreadPoolExecutor(max_workers=LOOKUP_THREADS) as pool:
        list(pool.map(one, pairs.items()))
    save_cache()
    print(f"  done ({total} markets)\n")


def fetch_history(wallet, limit):
    """Past trades for one wallet, oldest first."""
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
        time.sleep(0.2)
    return rows


def _parse_ts(value):
    """ISO date or epoch -> epoch seconds, or None."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _gamma_market(slug, token_id=None):
    """
    Fetch a market, settled or not.

    Gamma's default /markets query only returns markets that are still open —
    ask it for a finished sports fixture by slug and you get an empty list,
    which is why 2,880 of 3,242 markets came back "unresolved" when they had
    actually settled weeks earlier. closed=true is what reveals them.

    Settled is tried first because that's the common case when replaying
    history; the plain query is the fallback for markets still trading.
    """
    for params in ({"slug": slug, "limit": 1, "closed": "true"},
                   {"slug": slug, "limit": 1}):
        if not slug:
            break
        rows = pm._rows(pm._get(f"{GAMMA}/markets", params, tries=1))
        if rows:
            return rows[0]
    if token_id:
        rows = pm._rows(pm._get(f"{GAMMA}/markets",
                                {"clob_token_ids": token_id, "limit": 1,
                                 "closed": "true"}, tries=1))
        if rows:
            return rows[0]
    return None


def _outcome_for_token(market, token_id):
    """Did this token's side win? 1.0, 0.0, or None."""
    prices = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            return None
    if not prices:
        return None

    idx = 0
    ids = market.get("clobTokenIds") or market.get("clob_token_ids")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except Exception:
            ids = None
    if isinstance(ids, list) and token_id in ids:
        idx = ids.index(token_id)

    try:
        return 1.0 if float(prices[idx]) >= 0.5 else 0.0
    except (ValueError, IndexError, TypeError):
        return None


def resolution_info(token_id, slug=None):
    """(final_value, resolved_at) for one token. Cached."""
    if token_id in _resolution_cache:
        e = _resolution_cache[token_id]
        return e[0], e[1]

    value = None
    resolved_at = None

    m = _gamma_market(slug, token_id)
    if m:
        resolved_at = (_parse_ts(m.get("endDate"))
                       or _parse_ts(m.get("end_date_iso"))
                       or _parse_ts(m.get("endDateIso"))
                       or _parse_ts(m.get("closedTime")))
        if m.get("closed") or m.get("resolved"):
            value = _outcome_for_token(m, token_id)

    if value is None:
        price = pm.fetch_price(token_id, "SELL")
        if price is not None:
            if price >= 0.995:
                value = 1.0
            elif price <= 0.005:
                value = 0.0

    _resolution_cache[token_id] = (value, resolved_at, slug)
    if slug:
        pm._end_cache[slug] = resolved_at
    return value, resolved_at


def resolved_value(token_id, slug=None):
    return resolution_info(token_id, slug)[0]


def cached(token):
    """(value, resolved_at) for a token we've already looked up."""
    entry = _resolution_cache.get(token)
    if not entry:
        return (None, None)
    return entry[0], entry[1]


def main():
    per_wallet = int(sys.argv[1]) if len(sys.argv) > 1 else 500

    print("Backtest — replaying real history through the live bot's rules\n")
    print(f"  ${config.TRADING_ALLOCATION:.0f} allocation, "
          f"${config.TRADE_SIZE:.0f} per trade, "
          f"{config.MAX_POSITION_PCT*100:.0f}% per-market cap")
    print(f"  buy only between {config.MIN_BUY_PRICE} and {config.MAX_BUY_PRICE}, "
          f"max {config.MAX_POSITIONS_PER_TRADER} positions per trader")
    print(f"  only markets settling between {config.MIN_MARKET_HOURS:g}h "
          f"and {config.MAX_MARKET_DAYS:g}d away\n")

    traders = pm.fetch_top_traders()
    if not traders:
        print("leaderboard unreachable — try again")
        return 1
    print(f"{len(traders)} traders from the leaderboard\n")

    timeline = []
    for i, tr in enumerate(traders, 1):
        rows = fetch_history(tr["wallet"], per_wallet)
        print(f"  [{i:>2}/{len(traders)}] {tr['name'][:22]:22} {len(rows):>5} trades")
        for r in rows:
            r["_trader"] = tr
            timeline.append(r)
        time.sleep(0.2)

    if not timeline:
        print("\nno history returned")
        return 1

    timeline.sort(key=lambda r: float(r.get("timestamp") or 0))
    span_start = float(timeline[0].get("timestamp") or 0)
    span_end = float(timeline[-1].get("timestamp") or 0)
    days = (span_end - span_start) / 86400 if span_end > span_start else 0
    print(f"\n{len(timeline)} trades over {days:.1f} days\n")

    load_cache()
    prefetch(timeline)

    # ---- replay through the real rules -------------------------------------
    from bot import copybot

    s = {
        "cash": config.TRADING_ALLOCATION,
        "positions": {}, "realized_pnl": 0.0,
        "trades_executed": 0, "wins": 0, "losses": 0,
        "skipped": 0, "failed": 0,
    }
    skips = defaultdict(int)
    per_trader = defaultdict(lambda: {"copied": 0, "closed": 0,
                                      "wins": 0, "pnl": 0.0})
    closes = []
    run_ctx = {"new_positions": 0}
    last_bucket = None

    pending = {}          # token -> (final_value, resolved_at)

    def settle_due(now_ts):
        """Close any position whose market resolved before this point."""
        for tok in list(s["positions"].keys()):
            value, at = pending.get(tok, (None, None))
            if value is None or at is None or at > now_ts:
                continue
            pos = s["positions"][tok]
            proceeds = pos["shares"] * value
            pnl = proceeds - pos["cost"]
            s["cash"] += proceeds
            s["realized_pnl"] += pnl
            t = per_trader[pos["trader"]]
            t["closed"] += 1
            t["pnl"] += pnl
            t["wins"] += 1 if pnl >= 0 else 0
            closes.append(("resolved", pnl))
            del s["positions"][tok]

    for row in timeline:
        now_ts = float(row.get("timestamp") or 0)

        # Markets that resolved before this moment free their capital and
        # their slot, exactly as they would have at the time.
        settle_due(now_ts)

        # pacing is per run; a run here is a 30-second bucket of history
        bucket = int(now_ts // 30)
        if bucket != last_bucket:
            run_ctx = {"new_positions": 0}
            last_bucket = bucket

        trader = row["_trader"]
        side = (row.get("side") or "").upper()
        usd = float(row.get("usdcSize") or row.get("size") or 0)
        if usd < config.MIN_TRADE_USD:
            continue

        if side == "BUY":
            status, detail = copybot.handle_buy(s, row, trader, run_ctx,
                                                now_ts=now_ts)
            if status == "executed":
                per_trader[trader["name"]]["copied"] += 1
                tok = row.get("asset")
                if tok not in pending:
                    pending[tok] = cached(tok)
        elif side == "SELL":
            status, detail = copybot.handle_sell(s, row, trader)
            if status == "executed":
                pnl = detail["pnl"]
                t = per_trader[trader["name"]]
                t["closed"] += 1
                t["pnl"] += pnl
                t["wins"] += 1 if pnl >= 0 else 0
                closes.append(("sold", pnl))
        else:
            continue

        if status == "skipped":
            reason = detail if isinstance(detail, str) else "other"
            if "above" in reason:
                reason = "price too high"
            elif "below" in reason:
                reason = "price too low"
            elif "from this trader" in reason:
                reason = "per-trader cap"
            elif "cap" in reason and "market already" in reason:
                reason = "per-market cap"
            skips[reason] += 1

    # ---- settle what's still open at real resolution ------------------------
    print("Settling whatever is still open...")
    unresolved = 0
    for token in list(s["positions"].keys()):
        pos = s["positions"][token]
        value = cached(token)[0]
        if value is None:
            unresolved += 1
            continue
        proceeds = pos["shares"] * value
        pnl = proceeds - pos["cost"]
        s["cash"] += proceeds
        s["realized_pnl"] += pnl
        t = per_trader[pos["trader"]]
        t["closed"] += 1
        t["pnl"] += pnl
        t["wins"] += 1 if pnl >= 0 else 0
        closes.append(("resolved", pnl))
        del s["positions"][token]

    still_open_cost = sum(p["cost"] for p in s["positions"].values())
    equity = s["cash"] + still_open_cost
    start = config.TRADING_ALLOCATION

    # ---- report -------------------------------------------------------------
    print(f"\n{'='*74}")
    print(f"RESULT  ${start:.2f}  ->  ${equity:.2f}   "
          f"({(equity-start)/start*100:+.1f}%)  over {days:.1f} days")
    print(f"{'='*74}\n")

    wins = sum(1 for _, p in closes if p >= 0)
    n = len(closes)
    gains = [p for _, p in closes if p >= 0]
    losses = [p for _, p in closes if p < 0]
    print(f"  closed trades     {n}")
    print(f"  win rate          {wins/n*100:.1f}%" if n else "  win rate          -")
    print(f"  average win       ${sum(gains)/len(gains):.2f}" if gains else "")
    print(f"  average loss      ${sum(losses)/len(losses):.2f}" if losses else "")
    print(f"  realised P&L      ${s['realized_pnl']:+.2f}")
    if unresolved:
        print(f"  still unresolved  {unresolved} position(s), "
              f"${still_open_cost:.2f} tied up (counted at cost)")
    print()

    if skips:
        print("  trades the rules rejected:")
        for why, c in sorted(skips.items(), key=lambda x: -x[1]):
            print(f"    {c:>6}  {why}")
        print()

    rows = sorted(per_trader.items(), key=lambda kv: kv[1]["pnl"], reverse=True)
    print(f"  {'TRADER':24}{'COPIED':>8}{'CLOSED':>8}{'WIN%':>8}{'P&L':>10}")
    print("  " + "-" * 58)
    for name, r in rows:
        wr = f"{r['wins']/r['closed']*100:.0f}%" if r["closed"] else "-"
        print(f"  {name[:23]:24}{r['copied']:>8}{r['closed']:>8}{wr:>8}{r['pnl']:>10.2f}")

    print(f"""
  Read this as a ceiling, not a forecast. It fills at their exact price,
  your copy lands seconds later and worse. It covers {days:.0f} days, which is
  short. And these are the wallets on the leaderboard today — the ones who
  blew up aren't in the list.""")
    if n < 30:
        print(f"\n  Only {n} closed trades. That is too few to conclude anything;\n"
              f"  run it again with a larger history: python3 backtest.py 2000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
