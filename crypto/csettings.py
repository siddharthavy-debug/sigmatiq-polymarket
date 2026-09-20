"""
Dashboard settings for the crypto engine.

The rule that matters here: a settings file we cannot read means CHANGE
NOTHING. Last week a truncated read looked identical to "nothing saved", the
allocation silently reverted to the workflow default, and the difference was
taken out of cash and floored at zero. Money disappeared with no record.

So: absent file -> defaults, unreadable file -> leave everything alone.
"""
import json
import os

from . import cconfig

LOAD_FAILED = False

# name in the file -> (attribute on cconfig, type, min, max)
ALLOWED = {
    "mode":            ("MODE", str, None, None),
    "paused":          ("PAUSED", bool, None, None),
    "total_balance":   ("TOTAL_BALANCE", float, 10.0, 1_000_000.0),
    "allocation":      ("TRADING_ALLOCATION", float, 10.0, 1_000_000.0),
    "stake_pct":       ("STAKE_PCT", float, 0.002, 0.10),
    "max_deployed":    ("MAX_DEPLOYED_PCT", float, 0.02, 1.00),
    "daily_stop":      ("DAILY_STOP_PCT", float, 0.01, 0.50),
    "max_open":        ("MAX_OPEN_POSITIONS", int, 1, 60),
    "min_horizon":     ("MIN_HORIZON_SECONDS", int, 60, 86400),
    "max_horizon":     ("MAX_HORIZON_SECONDS", int, 300, 604800),
    "max_price":       ("MAX_BUY_PRICE", float, 0.05, 0.95),
    "min_price":       ("MIN_BUY_PRICE", float, 0.01, 0.90),
    "max_signal_age":  ("MAX_SIGNAL_AGE_SECONDS", float, 1.0, 120.0),
    "min_their_usd":   ("MIN_THEIR_TRADE_USD", float, 0.0, 100_000.0),
}


def load():
    global LOAD_FAILED
    LOAD_FAILED = False
    if not os.path.exists(cconfig.SETTINGS_FILE):
        return {}
    try:
        with open(cconfig.SETTINGS_FILE) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            LOAD_FAILED = True
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        LOAD_FAILED = True
        return {}


def apply():
    """
    Push saved settings onto cconfig. Called at the start of every decision,
    never cached — change a number on the dashboard and the next trade uses it.

    Returns the dict of what is actually live, which is what the dashboard
    reads back, so a change that didn't take is visible immediately instead of
    being discovered days later.
    """
    saved = load()
    if LOAD_FAILED:
        return {}, True          # (nothing applied, unreadable)

    live = {}
    for key, value in saved.items():
        spec = ALLOWED.get(key)
        if not spec:
            continue
        attr, kind, low, high = spec
        try:
            if kind is bool:
                value = bool(value)
            elif kind is str:
                value = str(value)
            else:
                value = kind(value)
        except (TypeError, ValueError):
            continue
        if kind in (int, float):
            if low is not None:
                value = max(low, value)
            if high is not None:
                value = min(high, value)
        if key == "mode":
            value = "live" if value.lower() == "live" else "paper"
            if value == "live" and not os.getenv("POLY_PRIVATE_KEY"):
                value = "paper"
                live["mode_downgraded"] = True
        setattr(cconfig, attr, value)
        live[key] = value

    # a price floor above the ceiling would silently refuse every trade
    if cconfig.MIN_BUY_PRICE >= cconfig.MAX_BUY_PRICE:
        cconfig.MIN_BUY_PRICE = max(0.01, cconfig.MAX_BUY_PRICE - 0.05)
        live["min_price"] = cconfig.MIN_BUY_PRICE
    if cconfig.MIN_HORIZON_SECONDS >= cconfig.MAX_HORIZON_SECONDS:
        cconfig.MIN_HORIZON_SECONDS = max(60, cconfig.MAX_HORIZON_SECONDS // 2)
        live["min_horizon"] = cconfig.MIN_HORIZON_SECONDS

    return live, False
