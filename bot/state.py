"""Everything the bot remembers between runs. Plain JSON, no database."""
import json
import os
from datetime import datetime, timezone

from . import config


def now():
    return datetime.now(timezone.utc).isoformat()


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _save(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def load_state():
    return _load(config.STATE_FILE, {
        "mode": config.MODE,
        "allocation": config.TRADING_ALLOCATION,
        "total_balance": config.TOTAL_BALANCE,
        "cash": config.TRADING_ALLOCATION,     # only ever the allocation
        "positions": {},
        "seen_trades": [],
        "traders_updated": None,
        "last_run": None,
        "realized_pnl": 0.0,
        "trades_executed": 0,
        "wins": 0,
        "losses": 0,
        "skipped": 0,
        "failed": 0,
        "initialised": False,
    })


def save_state(s):
    s["last_run"] = now()
    if len(s["seen_trades"]) > 8000:
        s["seen_trades"] = s["seen_trades"][-8000:]
    _save(config.STATE_FILE, s)


def load_trades():
    return _load(config.TRADES_FILE, [])


def append_trade(entry):
    trades = load_trades()
    entry["timestamp"] = now()
    trades.append(entry)
    _save(config.TRADES_FILE, trades)


def load_traders():
    return _load(config.TRADERS_FILE, {"updated": None, "traders": [], "benched": []})


def save_traders(obj):
    obj["updated"] = now()
    _save(config.TRADERS_FILE, obj)


def save_analysis(obj):
    _save(config.ANALYSIS_FILE, obj)
