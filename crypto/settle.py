"""
Turning recorded trades into settled results.

Polymarket's public API can't tell us who won — redeemed positions are deleted
from /positions, which is why ranking sports traders was such a fight. Here we
sidestep it entirely: we watched the trade happen, so once the market resolves
we know the outcome and we know the entry. The history becomes ours.

Netting matters. A wallet that buys 10 shares at 40c and sells 6 at 60c before
resolution has banked something regardless of the result, and pretending they
held to the end would misjudge them. So each wallet's position in a market is
netted:

    profit = (what they sold for) + (what the remainder settled at) - (cost)
"""
import time
from collections import defaultdict

from . import api as pm
from . import store

# A market can take a moment to publish its result after the clock runs out.
SETTLE_GRACE_SECONDS = 120
# Give up on a market that never resolves rather than carrying it forever.
ABANDON_AFTER_SECONDS = 6 * 3600

RESOLVED_HIGH = 0.995


def winning_asset(slug):
    """
    Which token id won. None if the market hasn't resolved yet.

    Gamma hides settled markets from the default query, so closed=true comes
    first here — the opposite of the live bot, which mostly asks about markets
    that are still trading.
    """
    rows = None
    for params in ({"slug": slug, "limit": 1, "closed": "true"},
                   {"slug": slug, "limit": 1}):
        rows = pm._rows(pm._get(f"{pm.GAMMA_API}/markets", params, tries=1))
        if rows:
            break
    if not rows:
        return None

    m = rows[0]
    prices = m.get("outcomePrices")
    tokens = m.get("clobTokenIds")
    if isinstance(prices, str):
        import json
        try:
            prices = json.loads(prices)
        except ValueError:
            return None
    if isinstance(tokens, str):
        import json
        try:
            tokens = json.loads(tokens)
        except ValueError:
            return None
    if not prices or not tokens or len(prices) != len(tokens):
        return None

    # The index of the 1.0 price is the index of the winning token. Getting
    # this pairing backwards would turn every win into a loss, so it is read
    # off clobTokenIds rather than assumed to be [Yes, No].
    for i, p in enumerate(prices):
        try:
            if float(p) >= RESOLVED_HIGH:
                return str(tokens[i])
        except (TypeError, ValueError):
            continue
    return None


def net_positions(trades):
    """
    Collapse raw trades into one position per (wallet, asset).

    Returns [{wallet, name, asset, shares_held, cost, sold, price, ts, coin}].
    """
    books = defaultdict(lambda: {"bought": 0.0, "sold": 0.0, "cost": 0.0,
                                 "proceeds": 0.0, "ts": None, "name": None,
                                 "coin": None, "outcome_index": None})
    for t in trades:
        b = books[(t["wallet"], t.get("asset"))]
        b["name"] = b["name"] or t.get("name")
        b["coin"] = b["coin"] or t.get("coin")
        b["outcome_index"] = t.get("outcome_index")
        if b["ts"] is None or t["ts"] < b["ts"]:
            b["ts"] = t["ts"]
        if (t.get("side") or "BUY").upper() == "SELL":
            b["sold"] += t["shares"]
            b["proceeds"] += t["shares"] * t["price"]
        else:
            b["bought"] += t["shares"]
            b["cost"] += t["shares"] * t["price"]

    out = []
    for (wallet, asset), b in books.items():
        if b["bought"] <= 0:
            continue            # sold something they bought before we watched
        held = max(b["bought"] - b["sold"], 0.0)
        out.append({
            "wallet": wallet,
            "name": b["name"],
            "asset": asset,
            "coin": b["coin"],
            "outcome_index": b["outcome_index"],
            "shares": b["bought"],
            "held": held,
            "cost": b["cost"],
            "proceeds": b["proceeds"],
            "price": b["cost"] / b["bought"],
            "ts": b["ts"] or time.time(),
        })
    return out


def settle_due(pending, wallets, now=None, verbose=True):
    """
    Settle every pending market whose result is in. Mutates both dicts.

    Returns (markets_settled, positions_folded, still_pending).
    """
    now = now or time.time()
    settled_markets = 0
    folded = 0

    for slug in list(pending.keys()):
        entry = pending[slug]
        end = entry.get("end")

        if end and now < end + SETTLE_GRACE_SECONDS:
            continue

        if end is None:
            end = pm.fetch_market_end(slug)
            if end:
                entry["end"] = end
                continue
            # no end date and nothing to settle against
            if now - entry.get("first_seen", now) > ABANDON_AFTER_SECONDS:
                pending.pop(slug, None)
            continue

        win = winning_asset(slug)
        if win is None:
            if now - end > ABANDON_AFTER_SECONDS:
                pending.pop(slug, None)
                if verbose:
                    print(f"  ! {slug} never resolved — dropped", flush=True)
            continue

        positions = net_positions(entry.get("trades", []))
        for p in positions:
            won = str(p["asset"]) == win
            payout = p["held"] * (1.0 if won else 0.0)
            profit = p["proceeds"] + payout - p["cost"]
            store.fold(wallets, p, won, profit)
            folded += 1

        store.mark_market_participation(wallets, positions)
        pending.pop(slug, None)
        settled_markets += 1

    return settled_markets, folded, len(pending)
