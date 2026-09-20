"""
Dashboard settings for the crypto engine.

Two rules earned the hard way.

A settings file we cannot read means CHANGE NOTHING. A truncated read used to
look identical to "nothing saved": config fell back to the workflow defaults,
the engine saw the allocation drop, took the difference out of cash and
floored it at zero. Money vanished with no record.

And on GitHub Actions the bot reads a checkout taken when the job started, so
a change saved from the dashboard never reached it — except by accident, when
a state push got rejected and the rebase dragged the new file in. So in
Actions the file is read straight from GitHub instead.
"""
import json
import os
import time

from . import cconfig

LOAD_FAILED = False

# Consulted before every decision, and the feed delivers dozens a second, so
# the remote copy is cached briefly.
REMOTE_TTL = float(os.getenv("CRYPTO_SETTINGS_TTL", "15"))
_remote = {"at": 0.0, "data": None}

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


def _remote_url():
    repo = os.getenv("GITHUB_REPOSITORY")
    if not repo or os.getenv("GITHUB_ACTIONS") != "true":
        return None
    branch = os.getenv("GITHUB_REF_NAME") or "main"
    return (f"https://raw.githubusercontent.com/{repo}/{branch}/"
            f"{cconfig.SETTINGS_FILE}")


def _fetch_remote():
    """The dashboard's copy. None when there isn't one to be had."""
    url = _remote_url()
    if not url:
        return None

    now = time.time()
    if _remote["data"] is not None and now - _remote["at"] < REMOTE_TTL:
        return _remote["data"]

    try:
        import requests
        r = requests.get(f"{url}?t={int(now)}", timeout=8,
                         headers={"Cache-Control": "no-cache"})
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                _remote["data"] = data
                _remote["at"] = now
                return data
    except Exception:
        pass

    # A fetch that failed must not look like "no settings". Keep whatever we
    # last had, and let the caller fall back to the local file.
    _remote["at"] = now
    return _remote["data"]


def _read_local():
    global LOAD_FAILED
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
        # a truncated read while the file is being committed lands here
        LOAD_FAILED = True
        return {}


def load():
    global LOAD_FAILED
    LOAD_FAILED = False
    remote = _fetch_remote()
    if remote is not None:
        return remote
    return _read_local()


def apply():
    """
    Push saved settings onto cconfig. Called before every decision, never
    cached — change a number on the dashboard and the next trade uses it.

    Returns (what is live, whether the file was unreadable). The dashboard
    reads the first back, so a change that didn't take is visible at once
    rather than days later.
    """
    saved = load()
    if LOAD_FAILED:
        return {}, True

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

    # A floor above the ceiling would silently refuse every trade.
    if cconfig.MIN_BUY_PRICE >= cconfig.MAX_BUY_PRICE:
        cconfig.MIN_BUY_PRICE = max(0.01, cconfig.MAX_BUY_PRICE - 0.05)
        live["min_price"] = cconfig.MIN_BUY_PRICE
    if cconfig.MIN_HORIZON_SECONDS >= cconfig.MAX_HORIZON_SECONDS:
        cconfig.MIN_HORIZON_SECONDS = max(60, cconfig.MAX_HORIZON_SECONDS // 2)
        live["min_horizon"] = cconfig.MIN_HORIZON_SECONDS

    return live, False
