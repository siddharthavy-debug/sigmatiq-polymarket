"""State and trade-log persistence for the crypto engine."""
import json
import os

from . import cconfig, engine


def _read(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)          # atomic: a reader never sees half a file


def load():
    s = _read(cconfig.STATE_FILE, None)
    if not isinstance(s, dict) or "positions" not in s:
        return engine.new_state()
    s.setdefault("copied", {})
    s.setdefault("contributed", s.get("allocation", 0.0))
    s.setdefault("day_realized", 0.0)
    return s


def save(s):
    _write(cconfig.STATE_FILE, s)


def load_pinned():
    """{lowercase wallet: {...}} from the weekly ranking."""
    book = _read(cconfig.PINNED_FILE, {})
    rows = book.get("traders", book) if isinstance(book, dict) else book
    out = {}
    if isinstance(rows, list):
        for r in rows:
            w = (r.get("wallet") or "").lower()
            if w:
                out[w] = r
    return out


def log(entries):
    """Append to the trade log, newest last, bounded."""
    if not entries:
        return
    rows = _read(cconfig.TRADES_FILE, [])
    if not isinstance(rows, list):
        rows = []
    rows.extend(entries)
    if len(rows) > cconfig.MAX_TRADE_LOG:
        rows = rows[-cconfig.MAX_TRADE_LOG:]
    _write(cconfig.TRADES_FILE, rows)
