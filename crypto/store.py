"""
Storage for the crypto recorder.

The feed publishes roughly 97,000 crypto trades a day. Writing every one of
them into git would make the repo unusable within a week, so nothing raw is
kept for long:

    pending.json   trades whose market hasn't settled yet. Small and churning —
                   a 5-minute market is pending for 5 minutes.
    wallets.json   the permanent record: one entry per wallet, with per-day
                   buckets so "recent form" and "consistency" are measurable
                   later without keeping the trades themselves.

Once a market settles, its trades are folded into the wallet aggregates and
deleted. The history we keep is the conclusions, not the raw feed.
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

DATA_DIR = os.path.join("data", "crypto")
PENDING = os.path.join(DATA_DIR, "pending.json")
WALLETS = os.path.join(DATA_DIR, "wallets.json")

# A wallet seen once is noise. Prune anything below this when the file grows,
# so we don't carry tens of thousands of one-trade tourists forever.
PRUNE_BELOW_TRADES = 3
PRUNE_WHEN_OVER = 30000


def _load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)


def load_pending():
    """{slug: {"end": epoch, "trades": [...] }}"""
    return _load(PENDING, {})


def save_pending(pending):
    _save(PENDING, pending)


def load_wallets():
    return _load(WALLETS, {})


def save_wallets(wallets):
    if len(wallets) > PRUNE_WHEN_OVER:
        wallets = {w: v for w, v in wallets.items()
                   if v.get("n", 0) >= PRUNE_BELOW_TRADES}
    _save(WALLETS, wallets)
    return wallets


def _blank(name):
    return {
        "name": name,
        "n": 0,            # settled trades
        "wins": 0,
        "cost": 0.0,       # total staked
        "profit": 0.0,     # total profit/loss
        "entry_sum": 0.0,  # to derive average entry price
        "days": {},        # "2026-09-17": {"n":, "wins":, "cost":, "profit":}
        "coins": {},       # "btc": count
        "both_sides": 0,   # markets where they took Up AND Down  -> market maker
        "markets": 0,
    }


def fold(wallets, trade, won, profit):
    """Add one settled trade into a wallet's running record."""
    w = trade["wallet"]
    rec = wallets.get(w)
    if rec is None:
        rec = wallets[w] = _blank(trade.get("name") or w[:10])

    day = datetime.fromtimestamp(trade["ts"], timezone.utc).strftime("%Y-%m-%d")
    d = rec["days"].get(day)
    if d is None:
        d = rec["days"][day] = {"n": 0, "wins": 0, "cost": 0.0, "profit": 0.0}

    cost = trade["shares"] * trade["price"]
    rec["n"] += 1
    rec["cost"] += cost
    rec["profit"] += profit
    rec["entry_sum"] += trade["price"]
    d["n"] += 1
    d["cost"] += cost
    d["profit"] += profit
    if won:
        rec["wins"] += 1
        d["wins"] += 1

    coin = trade.get("coin") or "?"
    rec["coins"][coin] = rec["coins"].get(coin, 0) + 1

    # keep the per-day detail bounded — 60 days is more than any ranking needs
    if len(rec["days"]) > 60:
        for old in sorted(rec["days"])[:-60]:
            rec["days"].pop(old, None)
    return rec


def mark_market_participation(wallets, slug_trades):
    """
    For one settled market: note who traded it, and who traded BOTH sides.

    Taking Up and Down in the same market within minutes is the market-maker
    signature. They're quoting a spread, not predicting anything, and copying
    one leg of that is taking the worse half of a position they never held.
    """
    sides = defaultdict(set)
    for t in slug_trades:
        sides[t["wallet"]].add(t.get("outcome_index"))
    for wallet, seen in sides.items():
        rec = wallets.get(wallet)
        if rec is None:
            continue
        rec["markets"] += 1
        if len(seen) > 1:
            rec["both_sides"] += 1
