"""State and trade-log persistence for the crypto engine."""
import json
import os

from . import cconfig, engine


class StateUnreadable(Exception):
    """The state file exists but will not parse. Never treat this as 'no state'."""


def _read(path, default, strict=False):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        # A truncated or half-written file looks EXACTLY like "nothing saved".
        # Treating it as a fresh start silently resets equity to the allocation,
        # zeroes realised P&L and throws away every open position -- and the
        # next save writes that over the real history. Money vanished this way
        # once already. A file we cannot read means stop, not start over.
        if strict:
            raise StateUnreadable(f"{path}: {e}") from e
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)          # atomic: a reader never sees half a file


def load():
    s = _read(cconfig.STATE_FILE, None, strict=True)
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
