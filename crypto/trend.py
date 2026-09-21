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
def recent_crypto_results(limit):
    """The last `limit` settled crypto UP/DOWN markets, newest first."""
    out, offset = [], 0
    while len(out) < limit and offset < 1500:
        rows = pm._rows(pm._get(f"{pm.GAMMA_API}/markets",
                                {"closed": "true", "limit": 200, "offset": offset,
                                 "order": "endDate", "ascending": "false"}))
        if not rows:
            break
        offset += 200
        for m in rows:
            slug = m.get("slug") or ""
            if not feed.CRYPTO_SLUG.match(slug):
                continue
            prices = pm._maybe_json(m.get("outcomePrices"))
            outcomes = pm._maybe_json(m.get("outcomes"))
            if not prices or not outcomes:
                continue
            try:
                p = [float(x) for x in prices]
            except (TypeError, ValueError):
                continue
            if abs(sum(p) - 1.0) > 0.01 or max(p) < 0.99:
                continue
            out.append(outcomes[p.index(max(p))])
            if len(out) >= limit:
                break
        time.sleep(0.1)
    return out


def up_rate(results):
    if not results:
        return None
    return sum(1 for r in results if str(r).lower().startswith("up")) / len(results)


def open_crypto_markets():
    rows = pm._rows(pm._get(f"{pm.GAMMA_API}/markets",
                            {"closed": "false", "active": "true", "limit": 300,
                             "order": "endDate", "ascending": "true"}))
    return [m for m in (rows or []) if feed.CRYPTO_SLUG.match(m.get("slug") or "")]


def up_quote(m):
    """(token, mid, ask) for the Up side, or None."""
    outcomes = pm._maybe_json(m.get("outcomes"))
    tokens = pm._maybe_json(m.get("clobTokenIds"))
    prices = pm._maybe_json(m.get("outcomePrices"))
    if not (outcomes and tokens and prices) or len(outcomes) != len(tokens):
        return None
    idx = next((i for i, o in enumerate(outcomes)
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
    return tokens[idx], mid, (ask if ask and ask > 0 else None)


def window_of(slug):
    tail = slug.rsplit("-", 1)[-1]
    return tail if tail.isdigit() else None


# ------------------------------------------------------------------ engine
def stake(s):
    return round(min(equity(s) * STAKE_PCT, max(s["cash"], 0.0)), 2)


def deployed(s):
    return sum(p["cost"] for p in s["positions"].values())


def consider(s, m, rate, now):
    """Yes or no, and why. Network-free so it can be tested."""
    if rate is None:
        return None, "no recent results yet"
    if rate <= 0.5:
        return None, f"trend is down ({rate*100:.0f}% up) — sitting out"

    q = up_quote(m)
    if not q:
        return None, "no Up quote"
    token, mid, ask = q
    if token in s["positions"]:
        return None, "already hold it"
    # We would post at mid and wait, so mid is the decision price. The ask is
    # recorded alongside so we learn what crossing would have cost.
    if not (BAND_LO <= mid <= BAND_HI):
        return None, f"{mid*100:.0f}c outside the band"

    slug = m.get("slug") or ""
    end = pm._parse_ts(m.get("endDate"))
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
def winning_outcome(slug):
    for params in ({"slug": slug, "limit": 1, "closed": "true"},
                   {"slug": slug, "limit": 1}):
        rows = pm._rows(pm._get(f"{pm.GAMMA_API}/markets", params, tries=1))
        if rows:
            break
    else:
        return None
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


def settle_due(s, log, now):
    closed = 0
    for token in list(s["positions"]):
        p = s["positions"][token]
        if now < p["end"] + SETTLE_GRACE:
            continue
        win = winning_outcome(p["slug"])
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


def main():
    s = load()
    log = []
    deadline = time.time() + WINDOW_MINUTES * 60
    last_commit = time.time()
    skips = {}

    print(f"=== trend bot | paper | ${s['bankroll']:.0f} ===", flush=True)
    print(f"  buy Up at {BAND_LO*100:.0f}-{BAND_HI*100:.0f}c when the last "
          f"{LOOKBACK} crypto markets were mostly Up", flush=True)
    print(f"  {STAKE_PCT*100:.1f}% a trade, {MAX_DEPLOYED*100:.0f}% deployed max, "
          f"listening {WINDOW_MINUTES:.0f} min", flush=True)
    if s["positions"]:
        print(f"  carrying {len(s['positions'])} position(s) from the last run",
              flush=True)

    while time.time() < deadline:
        now = time.time()
        settle_due(s, log, now)

        results = recent_crypto_results(LOOKBACK)
        rate = up_rate(results)
        if rate is not None:
            for m in open_crypto_markets():
                d, why = consider(s, m, rate, now)
                if d is None:
                    skips[why] = skips.get(why, 0) + 1
                    s["skipped"] += 1
                    continue
                p = open_position(s, d, now)
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
        _write(STATE, s)
        rows = _read(TRADES, [])
        if not isinstance(rows, list):
            rows = []
        rows.extend(log)
        _write(TRADES, rows[-MAX_LOG:])
        log = []

        eq = equity(s)
        print(f"  equity ${eq:.2f} | cash ${s['cash']:.2f} | "
              f"open {len(s['positions'])} | realised {s['realized_pnl']:+.2f} | "
              f"{s['wins']}W/{s['losses']}L | trend "
              f"{rate*100:.0f}% up" if rate is not None else "  (no trend yet)",
              flush=True)
        drift = books_drift(s)
        if abs(drift) > 0.01:
            print(f"  ! books out by {drift:+.2f}", flush=True)

        if time.time() - last_commit >= COMMIT_EVERY:
            if commit(datetime.now(timezone.utc).strftime("%H:%M")):
                print("  pushed", flush=True)
            last_commit = time.time()
        time.sleep(max(0, min(SCAN_EVERY, deadline - time.time())))

    settle_due(s, log, time.time())
    s["last_run"] = datetime.now(timezone.utc).isoformat()
    _write(STATE, s)
    rows = _read(TRADES, [])
    rows = (rows if isinstance(rows, list) else []) + log
    _write(TRADES, rows[-MAX_LOG:])
    commit("final")
    print(f"\nequity ${equity(s):.2f}  {s['executed']} trades  "
          f"{s['wins']}W/{s['losses']}L", flush=True)
    if skips:
        for k, v in sorted(skips.items(), key=lambda kv: -kv[1])[:6]:
            print(f"  skipped {v:>5}  {k}", flush=True)


if __name__ == "__main__":
    main()
