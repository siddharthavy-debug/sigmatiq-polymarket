"""
The crypto copy engine — decides, sizes, and books trades.

Kept deliberately free of networking and websockets so every rule in here can
be tested against made-up trades in a fraction of a second. The feed calls
consider(); everything else is arithmetic.

The rules, in the order they are applied:

    is this one of our traders        pinned list only
    is it a crypto UP/DOWN market     5 minutes to 1 hour
    is the signal fresh               a stale signal is a price that moved
    did they risk enough              their dust is not a view
    price inside the band             above 65c a win pays less than a loss
    have we copied them here already  one copy per trader per market
    are we already in this market     never two positions in one outcome
    is the day's loss limit hit       a bad run does not get fed
    is there room to deploy           20% of the pot at most
    do we have cash                   2% of the pot, compounding
"""
import time
from datetime import datetime, timezone

from . import cconfig, csettings, feed


# ---------------------------------------------------------------- the books
def new_state():
    return {
        "mode": cconfig.MODE,
        "allocation": cconfig.TRADING_ALLOCATION,
        "contributed": cconfig.TRADING_ALLOCATION,
        "total_balance": cconfig.TOTAL_BALANCE,
        "cash": cconfig.TRADING_ALLOCATION,
        "positions": {},          # token -> position
        "copied": {},             # "wallet|slug" -> timestamp
        "realized_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "executed": 0,
        "skipped": 0,
        "day": _today(),
        "day_start_equity": cconfig.TRADING_ALLOCATION,
        "day_realized": 0.0,
        "started": _now(),
        "last_run": None,
    }


def _now():
    return datetime.now(timezone.utc).isoformat()


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def equity(s):
    return s["cash"] + sum(p["cost"] for p in s["positions"].values())


def deployed(s):
    return sum(p["cost"] for p in s["positions"].values())


def stake(s):
    """
    2% of what the pot is worth right now — so it compounds on the way up and
    protects itself on the way down, with nobody touching a dial.
    """
    size = equity(s) * cconfig.STAKE_PCT
    return round(min(size, max(s["cash"], 0.0)), 2)


def roll_day(s):
    """A new UTC day resets the daily stop."""
    today = _today()
    if s.get("day") != today:
        s["day"] = today
        s["day_start_equity"] = equity(s)
        s["day_realized"] = 0.0
        return True
    return False


def day_loss_pct(s):
    start = s.get("day_start_equity") or 0.0
    if start <= 0:
        return 0.0
    return max(0.0, (start - equity(s)) / start)


def sync_allocation(s, unreadable):
    """
    Move real cash when the allocation changes, and keep the books exact.

    Two lessons are baked in here. A settings file we could not read must not
    look like a request to change anything. And taking out more than is in
    cash cannot silently destroy the difference — we remove what exists and
    adjust contributed capital to match, so equity always equals contributed
    plus realised.
    """
    previous = s.get("allocation", cconfig.TRADING_ALLOCATION)
    s.setdefault("contributed", previous)

    if unreadable:
        cconfig.TRADING_ALLOCATION = previous
        return None

    if abs(previous - cconfig.TRADING_ALLOCATION) < 1e-9:
        return None

    delta = cconfig.TRADING_ALLOCATION - previous
    cash = s.get("cash", 0.0)
    actual = delta if delta >= 0 else -min(-delta, cash)
    s["cash"] = cash + actual
    s["contributed"] += actual
    s["allocation"] = cconfig.TRADING_ALLOCATION

    # Moving capital in or out is not a profit or a loss, and the daily stop
    # must not read it as one. Without this, dropping the allocation from 300
    # to 100 looks like being down 66.7% on the day and the bot refuses to
    # trade until midnight.
    s["day_start_equity"] = max(0.0, s.get("day_start_equity", 0.0) + actual)
    return actual


def books_drift(s):
    """Equity minus (contributed + realised). Anything but zero is a bug."""
    return equity(s) - (s.get("contributed", s.get("allocation", 0.0))
                        + s.get("realized_pnl", 0.0))


# ------------------------------------------------------------- the decision
def _window_key(trade):
    """
    What makes two positions the same bet: the same settlement window and the
    same direction. btc-updown-5m-1789943100 Down and sol-updown-5m-1789943100
    Down both ride on whether crypto fell in those five minutes.
    """
    slug = trade.get("slug") or ""
    stamp = slug.rsplit("-", 1)[-1]
    if not stamp.isdigit():
        return None
    return f"{stamp}|{trade.get('outcome')}"


def consider(s, trade, pinned, now=None):
    """
    Should we copy this trade?

    trade is one record from the feed. pinned maps lowercase wallet -> info.
    Returns (action, reason, detail) where action is "copy" or "skip".
    """
    now = now or time.time()

    wallet = (trade.get("wallet") or "").lower()
    who = pinned.get(wallet)
    if not who:
        return "skip", "not one of ours", None

    if (trade.get("side") or "BUY").upper() != "BUY":
        return "skip", "they sold, not bought", None

    horizon = trade.get("horizon")
    if horizon is None:
        return "skip", "unknown market length", None
    if horizon < cconfig.MIN_HORIZON_SECONDS:
        return "skip", f"{horizon//60}m market, too short", None
    if horizon > cconfig.MAX_HORIZON_SECONDS:
        return "skip", f"{horizon//60}m market, too long", None

    age = now - (trade.get("ts") or now)
    if age > cconfig.MAX_SIGNAL_AGE_SECONDS:
        return "skip", f"signal {age:.0f}s old, price has moved", None

    their_usd = (trade.get("shares") or 0) * (trade.get("price") or 0)
    if their_usd < cconfig.MIN_THEIR_TRADE_USD:
        return "skip", f"they risked only ${their_usd:.0f}", None

    price = float(trade.get("price") or 0)
    if price <= 0:
        return "skip", "no price", None
    if price > cconfig.MAX_BUY_PRICE:
        return "skip", f"{price*100:.0f}c — a win pays less than a loss", None
    if price < cconfig.MIN_BUY_PRICE:
        return "skip", f"{price*100:.0f}c — longshot", None

    key = f"{wallet}|{trade.get('slug')}"
    if cconfig.ONE_COPY_PER_MARKET_PER_TRADER and key in s["copied"]:
        return "skip", "already copied them in this market", None

    theirs = sum(1 for p in s["positions"].values()
                 if (p.get("wallet") or "").lower() == wallet)
    if theirs >= cconfig.MAX_POSITIONS_PER_TRADER:
        return "skip", (f"already following {who.get('name') or wallet[:8]} "
                        f"into {theirs} markets"), None

    # Correlated exposure. Five coins settling in the same window, all bet the
    # same way, rise and fall together — so they are counted as one position,
    # not five. This is the rule that was missing when $50 went in one tick.
    window = _window_key(trade)
    if window:
        same = sum(1 for p in s["positions"].values()
                   if p.get("window") == window)
        if same >= cconfig.MAX_PER_WINDOW_DIRECTION:
            return "skip", (f"{same} already on {trade.get('outcome')} "
                            f"in this window — same bet, different coin"), None

    token = trade.get("asset")
    if not token:
        return "skip", "no token", None
    if token in s["positions"]:
        return "skip", "already hold this outcome", None

    if cconfig.PAUSED:
        return "skip", "paused", None

    loss = day_loss_pct(s)
    if cconfig.DAILY_STOP_PCT > 0 and loss >= cconfig.DAILY_STOP_PCT:
        return "skip", (f"daily stop — down {loss*100:.1f}% today, "
                        f"no new positions"), None

    if len(s["positions"]) >= cconfig.MAX_OPEN_POSITIONS:
        return "skip", f"{len(s['positions'])} positions already open", None

    size = stake(s)
    room = equity(s) * cconfig.MAX_DEPLOYED_PCT - deployed(s)
    if room < cconfig.MIN_STAKE_USD:
        return "skip", (f"${deployed(s):.0f} already at work of "
                        f"${equity(s)*cconfig.MAX_DEPLOYED_PCT:.0f} allowed"), None
    size = round(min(size, room), 2)

    if size < cconfig.MIN_STAKE_USD:
        return "skip", f"only ${s['cash']:.2f} cash left", None

    return "copy", "", {"size": size, "price": price, "token": token,
                        "trader": who.get("name") or wallet[:10],
                        "wallet": wallet, "key": key}


def open_position(s, trade, detail, now=None):
    """Book a copy. Caller has already placed the real order in live mode."""
    now = now or time.time()
    size = detail["size"]
    price = detail["price"]
    token = detail["token"]

    s["positions"][token] = {
        "market": trade.get("slug"),
        "slug": trade.get("slug"),
        "coin": trade.get("coin"),
        "outcome": trade.get("outcome"),
        "horizon": trade.get("horizon"),
        "shares": size / price,
        "cost": size,
        "price": price,
        "trader": detail["trader"],
        "wallet": detail["wallet"],
        "their_price": price,
        "window": _window_key(trade),
        "opened": now,
    }
    s["copied"][detail["key"]] = now
    s["cash"] -= size
    s["executed"] += 1
    return s["positions"][token]


def settle_position(s, token, won, now=None):
    """Market resolved. Winner pays 1.0 a share, loser pays nothing."""
    pos = s["positions"].pop(token, None)
    if pos is None:
        return None
    proceeds = pos["shares"] * (1.0 if won else 0.0)
    pnl = proceeds - pos["cost"]
    s["cash"] += proceeds
    s["realized_pnl"] += pnl
    s["day_realized"] = s.get("day_realized", 0.0) + pnl
    if won:
        s["wins"] += 1
    else:
        s["losses"] += 1
    pos["pnl"] = pnl
    pos["proceeds"] = proceeds
    pos["won"] = won
    pos["closed"] = now or time.time()
    return pos


def forget_old_copies(s, keep_hours=48):
    """The copied-here-already map should not grow forever."""
    cutoff = time.time() - keep_hours * 3600
    s["copied"] = {k: v for k, v in s["copied"].items() if v >= cutoff}
