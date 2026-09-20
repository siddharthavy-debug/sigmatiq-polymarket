"""
Runtime settings, editable from the dashboard.

config.py holds the defaults. This layer lets the dashboard override a few of
them by committing data/settings.json to the repo, which the bot re-reads at
the start of every pass. Without it the only way to change anything is editing
code and pushing.

Only these keys are honoured. Anything else in the file is ignored, so a
malformed or hostile settings file can't reach into the rest of the bot.
"""
import json
import os

from . import config

SETTINGS_FILE = "data/settings.json"

# key -> (config attribute, type, low, high)
ALLOWED = {
    "mode":             ("MODE", str, None, None),
    "paused":           (None, bool, None, None),
    "allocation":       ("TRADING_ALLOCATION", float, 5.0, 100000.0),
    "total_balance":    ("TOTAL_BALANCE", float, 5.0, 10000000.0),
    "trade_size":       ("TRADE_SIZE", float, 1.0, 10000.0),
    "max_position_pct": ("MAX_POSITION_PCT", float, 0.005, 1.0),
    "max_market_days":  ("MAX_MARKET_DAYS", float, 0.04, 400.0),
    "min_market_hours": ("MIN_MARKET_HOURS", float, 0.0, 240.0),
    "max_buy_price":    ("MAX_BUY_PRICE", float, 0.05, 0.99),
    "min_buy_price":    ("MIN_BUY_PRICE", float, 0.01, 0.95),
    "top_n_traders":    ("TOP_N_TRADERS", int, 1, 50),
    "max_per_trader":   ("MAX_POSITIONS_PER_TRADER", int, 1, 50),
}


# True when the settings file exists but could not be read this pass. It
# matters: a failed read used to look identical to "no settings saved", so
# config silently fell back to the workflow's environment defaults. With the
# allocation that was expensive — the bot saw the allocation drop from 305 to
# 100, moved the difference out of cash, and the floor at zero destroyed the
# balance. A file we cannot read means "change nothing", not "use defaults".
LOAD_FAILED = False


def load():
    global LOAD_FAILED
    LOAD_FAILED = False
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            LOAD_FAILED = True
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        # a truncated read while the file is being committed lands here
        LOAD_FAILED = True
        return {}


def apply():
    """
    Push saved settings onto config. Returns (applied_dict, paused_bool).

    Live mode is deliberately not something a dashboard click can fully switch
    on: the wallet key still has to be in GitHub Secrets, and without it the
    bot stays in paper mode no matter what this file says.
    """
    saved = load()
    applied = {}

    for key, value in saved.items():
        spec = ALLOWED.get(key)
        if not spec:
            continue
        attr, kind, low, high = spec
        if attr is None:
            continue
        try:
            value = kind(value)
        except (TypeError, ValueError):
            continue
        if kind is float or kind is int:
            if low is not None and value < low:
                value = low
            if high is not None and value > high:
                value = high
        if key == "mode":
            value = "live" if str(value).lower() == "live" else "paper"
            if value == "live" and not config.POLY_PRIVATE_KEY:
                # asked for live, no wallet available — stay on paper
                value = "paper"
                applied["mode_downgraded"] = True
        setattr(config, attr, value)
        applied[key] = value

    return applied, bool(saved.get("paused"))
