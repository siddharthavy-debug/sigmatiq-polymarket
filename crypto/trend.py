#!/usr/bin/env python3
"""
trend.py -- the trend bot. One rule, no traders, no copying.

    If the last N crypto UP/DOWN markets mostly resolved Up,
    buy Up in the 45-61c band.

Why this exists
---------------
Copy trading was measured across 15,230 markets and the crowd turned out to
carry no information: majority-Up calls beat their price by +0.1pp, majority-
Down by -5.0pp. All of it was explained by crypto drifting upward.

This rule was found the other way round -- chosen on the first half of that
history and then tested on the second half it had never seen: +5.3pp, t=2.27.
It beat plain "always buy Up" in all six time periods, and lost less in the
one period that lost. That is the only thing that has survived an honest
out-of-sample test.

The thing that decides whether it is real
-----------------------------------------
Spread. At mid the edge is +5.3pp; paying 3c over mid leaves +2.3pp and the
significance is gone. So every trade here records BOTH the mid and the ask.
After a day of live prices we will know what we would actually have paid,
which is the number the backtest could not give us.

Unlike the copy bot this strategy is never in a hurry -- "the last 20 were
mostly Up" stays true for minutes -- so it can afford to wait for a good
price instead of crossing the spread. That option is what may keep the edge
alive, and it is why the mid/ask gap is logged rather than assumed.

Paper only. Places no orders.

    python3 -m crypto.trend
"""
import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timezone

from . import api as pm
from . import feed

STATE = os.getenv("TREND_STATE", os.path.join("data", "trend_state.json"))
TRADES = os.getenv("TREND_TRADES", os.path.join("data", "trend_trades.json"))

BANKROLL = float(os.getenv("TREND_BANKROLL", "500"))
STAKE_PCT = float(os.getenv("TREND_STAKE_PCT", "0.02"))
LOOKBACK = int(os.getenv("TREND_LOOKBACK", "20"))
BAND_LO = float(os.getenv("TREND_BAND_LO", "0.45"))
BAND_HI = float(os.getenv("TREND_BAND_HI", "0.61"))
MAX_DEPLOYED = float(os.getenv("TREND_MAX_DEPLOYED", "0.60"))
MAX_OPEN = int(os.getenv("TREND_MAX_OPEN", "25"))
# One position per market window+direction, same reason the copy bot needed it:
# five coins settling together on the same move is one bet, not five.
MAX_PER_WINDOW = int(os.getenv("TREND_MAX_PER_WINDOW", "3"))
# Only the short markets the rule was tested on. A slug whose length cannot be
# read (a daily or weekly crypto market) used to default to 5 minutes, so the
# bot would buy a week-long market and immediately try to settle it.
MIN_HORIZON = int(os.getenv("TREND_MIN_HORIZON", "300"))
MAX_HORIZON = int(os.getenv("TREND_MAX_HORIZON", "3600"))

WINDOW_MINUTES = float(os.getenv("WINDOW_MINUTES", "330"))
SCAN_EVERY = int(os.getenv("TREND_SCAN_SECONDS", "45"))
COMMIT_EVERY = int(os.getenv("TREND_COMMIT_SECONDS", "300"))
MAX_LOG = int(os.getenv("TREND_MAX_LOG", "6000"))
SETTLE_GRACE = 120


# ----------------------------------------------------------------- storage
class StateUnreadable(Exception):
    """The state file exists but will not parse. Never start over from that."""


def _read(path, default, strict=False):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        if strict:
            raise StateUnreadable(f"{path}: {e}") from e
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)


def new_state():
    return {"bankroll": BANKROLL, "cash": BANKROLL, "contributed": BANKROLL,
            "positions": {}, "realized_pnl": 0.0,
            "wins": 0, "losses": 0, "executed": 0, "skipped": 0,
            "slippage_paid": 0.0,          # what the spread actually cost us
            "trend_up_pct": None, "trend_samples": 0, "markets_seen": 0,
            "started": datetime.now(timezone.utc).isoformat(), "last_run": None}


def load():
    s = _read(STATE, None, strict=True)
    if not isinstance(s, dict) or "positions" not in s:
        return new_state()
    s.setdefault("slippage_paid", 0.0)
    return s


def equity(s):
    return s["cash"] + sum(p["shares"] * p["price"] for p in s["positions"].values())


# ------------------------------------------------------------------ market
def market_end(slug, horizon):
    """When the window closes. The trailing stamp is the START -- verified
    against Gamma: btc-updown-5m-1789986300 runs 10:25 -> 10:30."""
    tail = slug.rsplit("-", 1)[-1]
    if not tail.isdigit():
        return None
    if horizon is None:
        return None
    return int(tail) + horizon


def resolve_slug(slug):
    """'Up', 'Down', or None if it has not settled. Slug lookups are the only
    Gamma query that works reliably here -- ordering and date filters on
    /markets return unrelated junk (2029 elections, table tennis)."""
    for params in ({"slug": slug, "limit": 1, "closed": "true"},
                   {"slug": slug, "limit": 1}):
        rows = pm._rows(pm._get(f"{pm.GAMMA_API}/markets", params, tries=1))
        if rows:
            break
    if not rows:
        return None
    m = rows[0]
    prices = pm._maybe_json(m.get("outcomePrices"))
    outcomes = pm._maybe_json(m.get("outcomes"))
    if not prices or not outcomes:
        return None
    try:
        p = [float(x) for x in prices]
    except (TypeError, ValueError):
        return None
    if abs(sum(p) - 1.0) > 0.01 or max(p) < 0.99:
        return None
    return outcomes[p.index(max(p))]


def quote_slug(slug):
    """(up_token, up_mid, up_ask) live from Gamma, or None.

    One call per candidate trade. The ask is what makes the spread
    measurable -- the feed only carries executed prices, not the book."""
    rows = pm._rows(pm._get(f"{pm.GAMMA_API}/markets", {"slug": slug, "limit": 1}))
    if not rows:
        return None
    m = rows[0]
    outcomes = pm._maybe_json(m.get("outcomes"))
    tokens = pm._maybe_json(m.get("clobTokenIds"))
    prices = pm._maybe_json(m.get("outcomePrices"))
    if not (outcomes and tokens and prices) or len(outcomes) != len(tokens):
        return None
    idx = next((k for k, o in enumerate(outcomes)
                if str(o).lower().startswith("up")), None)
    if idx is None:
        return None
    try:
        mid = float(prices[idx])
    except (TypeError, ValueError):
        return None
    ask = m.get("bestAsk")
    try:
        ask = float(ask)
    except (TypeError, ValueError):
        ask = None
    return tokens[idx], mid, (ask if ask and 0 < ask < 1 else None)


def up_rate(results):
    if not results:
        return None
    return sum(1 for r in results if str(r).lower().startswith("up")) / len(results)


def window_of(slug):
    tail = slug.rsplit("-", 1)[-1]
    return tail if tail.isdigit() else None


# ------------------------------------------------------------------ engine
def stake(s):
    return round(min(equity(s) * STAKE_PCT, max(s["cash"], 0.0)), 2)


def deployed(s):
    return sum(p["cost"] for p in s["positions"].values())


def consider(s, slug, horizon, quote, rate, now):
    """Yes or no, and why. Network-free so it can be tested."""
    if rate is None:
        return None, "no recent results yet"
    if rate <= 0.5:
        return None, f"trend is down ({rate*100:.0f}% up) — sitting out"
    if horizon is None:
        return None, "cannot tell how long this market runs"
    if not (MIN_HORIZON <= horizon <= MAX_HORIZON):
        return None, f"{horizon//60}m market outside the 5m-60m range"
    if not quote:
        return None, "no Up quote"
    token, mid, ask = quote
    if token in s["positions"]:
        return None, "already hold it"
    # We would post at mid and wait, so mid is the decision price. The ask is
    # recorded alongside so we learn what crossing would have cost.
    if not (BAND_LO <= mid <= BAND_HI):
        return None, f"{mid*100:.0f}c outside the band"

    end = market_end(slug, horizon)
    if not end or end - now < 60:
        return None, "too close to settlement"

    win = window_of(slug)
    if win and sum(1 for p in s["positions"].values()
                   if p.get("window") == win) >= MAX_PER_WINDOW:
        return None, "already loaded on this window"
    if len(s["positions"]) >= MAX_OPEN:
        return None, "max open positions"
    size = stake(s)
    if size < 1:
        return None, "no cash"
    if deployed(s) + size > equity(s) * MAX_DEPLOYED:
        return None, "deployment cap"
    return dict(token=token, slug=slug, mid=mid, ask=ask, size=size,
                end=end, window=win, coin=slug.split("-", 1)[0]), "ok"


def open_position(s, d, now):
    price = d["mid"]                       # we assume a fill at mid
    shares = d["size"] / price
    s["positions"][d["token"]] = {
        "slug": d["slug"], "coin": d["coin"], "outcome": "Up",
        "shares": shares, "cost": d["size"], "price": price,
        "ask_at_entry": d["ask"], "window": d["window"],
        "end": d["end"], "opened": now}
    s["cash"] -= d["size"]
    s["executed"] += 1
    # what crossing the spread would have cost, for the record
    if d["ask"]:
        s["slippage_paid"] += (d["ask"] - price) * shares
    return s["positions"][d["token"]]


def settle_position(s, token, won, now):
    p = s["positions"].pop(token)
    proceeds = p["shares"] if won else 0.0
    s["cash"] += proceeds
    pnl = proceeds - p["cost"]
    s["realized_pnl"] += pnl
    s["wins" if won else "losses"] += 1
    p["pnl"], p["proceeds"], p["won"] = pnl, proceeds, won
    return p


def books_drift(s):
    return equity(s) - (s["contributed"] + s["realized_pnl"])


# --------------------------------------------------------------------- run
def settle_due(s, log, now):
    closed = 0
    for token in list(s["positions"]):
        p = s["positions"][token]
        if now < p["end"] + SETTLE_GRACE:
            continue
        win = resolve_slug(p["slug"])
        if win is None:
            if now - p["end"] > 6 * 3600:
                s["positions"].pop(token, None)
                print(f"  ! {p['slug']} never resolved — dropped", flush=True)
            continue
        won = str(win).lower().startswith("up")
        done = settle_position(s, token, won, now)
        closed += 1
        log.append({"event": "settle", "market": done["slug"],
                    "coin": done["coin"], "price": round(done["price"], 4),
                    "ask_at_entry": done.get("ask_at_entry"),
                    "cost": round(done["cost"], 2),
                    "proceeds": round(done["proceeds"], 2),
                    "pnl": round(done["pnl"], 2),
                    "result": "WON" if won else "LOST",
                    "at": datetime.now(timezone.utc).isoformat()})
        print(f"  {'WON ' if won else 'LOST'} {done['pnl']:>+7.2f}  "
              f"{done['slug'][:34]}", flush=True)
    return closed


def _git(*a):
    return subprocess.run(("git",) + a, capture_output=True, text=True)


def commit(label):
    _git("config", "user.name", "trend-bot")
    _git("config", "user.email", "trend@users.noreply.github.com")
    _git("add", STATE, TRADES)
    if _git("diff", "--staged", "--quiet").returncode == 0:
        return False
    _git("commit", "-m", f"trend {label}")
    branch = os.getenv("GITHUB_REF_NAME") or "main"
    if _git("push", "origin", f"HEAD:{branch}").returncode == 0:
        return True
    _git("fetch", "origin", branch)
    _git("rebase", "--autostash", "-X", "theirs", f"origin/{branch}")
    return _git("push", "origin", f"HEAD:{branch}").returncode == 0


async def listen(bot_state, seen, deadline):
    """Discover live crypto markets from the feed.

    The markets endpoint cannot find these -- ordering returns 2029 elections
    and date filters return table tennis -- and the slugs are not created on
    every 5-minute boundary, so they cannot be constructed either. The feed is
    the only reliable way to learn which markets exist right now.
    """
    import websockets
    attempt = 0
    while time.time() < deadline:
        try:
            async with websockets.connect(
                    feed.URL, ping_interval=15, ping_timeout=45,
                    close_timeout=5, max_queue=2048) as ws:
                await ws.send(feed.subscribe_message())
                attempt = 0
                while time.time() < deadline:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(deadline - time.time(), 1))
                    try:
                        for t in feed.parse(raw):
                            slug = t["slug"]
                            if slug not in seen:
                                seen[slug] = {"horizon": t.get("horizon"),
                                              "coin": t["coin"],
                                              "first": time.time()}
                    except Exception as e:
                        print(f"  ! parse error: {type(e).__name__}: {e}", flush=True)
        except asyncio.TimeoutError:
            return
        except Exception as e:
            if time.time() >= deadline:
                return
            attempt += 1
            wait = min(2 ** attempt, 30)
            print(f"  reconnecting in {wait}s ({type(e).__name__})", flush=True)
            await asyncio.sleep(min(wait, max(deadline - time.time(), 0)))


async def trade_loop(s, seen, deadline):
    log, skips = [], {}
    resolved = []            # newest last: 'Up'/'Down' of markets we have seen
    checked = set()
    last_commit = time.time()

    while time.time() < deadline:
        await asyncio.sleep(min(SCAN_EVERY, max(deadline - time.time(), 0)))
        if time.time() >= deadline:
            break
        now = time.time()

        # settle our own positions
        await asyncio.to_thread(settle_due, s, log, now)

        # learn the trend from markets the feed showed us that have now ended
        due = [sl for sl, m in seen.items()
               if sl not in checked
               and (market_end(sl, m["horizon"]) or 0) + SETTLE_GRACE < now]
        due.sort(key=lambda sl: market_end(sl, seen[sl]["horizon"]) or 0)
        for sl in due[-40:]:
            w = await asyncio.to_thread(resolve_slug, sl)
            if w:
                checked.add(sl)
                resolved.append((market_end(sl, seen[sl]["horizon"]) or 0, str(w)))
            elif now - (market_end(sl, seen[sl]["horizon"]) or now) > 3600:
                # Give up only after an hour. Marking it checked on the FIRST
                # miss was the bug: Polymarket often publishes the result well
                # after the window closes, so nearly every market was discarded
                # unresolved and the trend never accumulated.
                checked.add(sl)
        resolved.sort()
        resolved = resolved[-200:]

        recent = [w for _, w in resolved[-LOOKBACK:]]
        rate = up_rate(recent) if len(recent) >= LOOKBACK else None
        # Into the state file so the trend is visible without reading the log.
        s["trend_up_pct"] = round(rate, 3) if rate is not None else None
        s["trend_samples"] = len(resolved)
        s["markets_seen"] = len(seen)

        if rate is not None and rate > 0.5:
            live = [sl for sl, m in seen.items()
                    if (market_end(sl, m["horizon"]) or 0) > now + 60]
            for sl in sorted(live, key=lambda x: seen[x]["first"], reverse=True)[:25]:
                q = await asyncio.to_thread(quote_slug, sl)
                d, why = consider(s, sl, seen[sl]["horizon"], q, rate, now)
                if d is None:
                    skips[why] = skips.get(why, 0) + 1
                    s["skipped"] += 1
                    continue
                open_position(s, d, now)
                log.append({"event": "buy", "market": d["slug"], "coin": d["coin"],
                            "mid": round(d["mid"], 4),
                            "ask": round(d["ask"], 4) if d["ask"] else None,
                            "spread_c": (round((d["ask"] - d["mid"]) * 100, 2)
                                         if d["ask"] else None),
                            "size": d["size"], "trend_up_pct": round(rate, 3),
                            "at": datetime.now(timezone.utc).isoformat()})
                print(f"  BUY  Up {d['mid']*100:>4.0f}c"
                      f"{('  ask ' + format(d['ask']*100, '.0f') + 'c') if d['ask'] else ''}"
                      f"  ${d['size']:.2f}  {d['slug'][:34]}", flush=True)

        s["last_run"] = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(_write, STATE, s)
        rows = _read(TRADES, [])
        if not isinstance(rows, list):
            rows = []
        rows.extend(log); log = []
        await asyncio.to_thread(_write, TRADES, rows[-MAX_LOG:])

        trend_txt = (f"{rate*100:.0f}% up ({len(recent)}/{LOOKBACK})"
                     if rate is not None
                     else f"learning ({len(resolved)}/{LOOKBACK} resolved)")
        print(f"  equity ${equity(s):.2f} | cash ${s['cash']:.2f} | "
              f"open {len(s['positions'])} | {s['wins']}W/{s['losses']}L | "
              f"seen {len(seen)} markets | trend {trend_txt}", flush=True)
        drift = books_drift(s)
        if abs(drift) > 0.01:
            print(f"  ! books out by {drift:+.2f}", flush=True)

        if time.time() - last_commit >= COMMIT_EVERY:
            if await asyncio.to_thread(
                    commit, datetime.now(timezone.utc).strftime("%H:%M")):
                print("  pushed", flush=True)
            last_commit = time.time()

    settle_due(s, log, time.time())
    s["last_run"] = datetime.now(timezone.utc).isoformat()
    _write(STATE, s)
    rows = _read(TRADES, [])
    _write(TRADES, ((rows if isinstance(rows, list) else []) + log)[-MAX_LOG:])
    commit("final")
    print(f"\nequity ${equity(s):.2f}  {s['executed']} trades  "
          f"{s['wins']}W/{s['losses']}L", flush=True)
    for k, v in sorted(skips.items(), key=lambda kv: -kv[1])[:6]:
        print(f"  skipped {v:>5}  {k}", flush=True)


async def main_async():
    s = load()
    seen = {}
    deadline = time.time() + WINDOW_MINUTES * 60
    print(f"=== trend bot | paper | ${s['bankroll']:.0f} ===", flush=True)
    print(f"  buy Up at {BAND_LO*100:.0f}-{BAND_HI*100:.0f}c when the last "
          f"{LOOKBACK} crypto markets were mostly Up", flush=True)
    print(f"  {STAKE_PCT*100:.1f}% a trade, {MAX_DEPLOYED*100:.0f}% deployed max, "
          f"listening {WINDOW_MINUTES:.0f} min", flush=True)
    if s["positions"]:
        print(f"  carrying {len(s['positions'])} position(s) from the last run",
              flush=True)
    print("  learning the trend from the live feed — no trades until "
          f"{LOOKBACK} markets have resolved", flush=True)
    await asyncio.gather(listen(s, seen, deadline),
                         trade_loop(s, seen, deadline))


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
