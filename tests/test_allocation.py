"""
The allocation bug, and the accounting that now makes it impossible.

A settings file that failed to parse used to look identical to "nothing
saved": config fell back to the workflow defaults, the engine saw the
allocation drop from 305 to 100, took the difference out of cash, and floored
the result at zero. Money vanished with no record — $32.58 of it.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crypto import cconfig, csettings, engine


def state(alloc, cash, contributed, positions=0.0):
    """Books that balance to start with: equity = contributed + realised."""
    s = engine.new_state()
    s.update({"allocation": alloc, "cash": cash, "contributed": contributed})
    if positions:
        s["positions"] = {"tok": {"cost": positions, "shares": 1.0,
                                  "price": 0.5, "trader": "x"}}
    s["realized_pnl"] = engine.equity(s) - contributed
    return s


def sync(s, new_alloc, unreadable):
    cconfig.TRADING_ALLOCATION = new_alloc
    return engine.sync_allocation(s, unreadable)


ok = 0

print("the bug: settings unreadable, defaults say 100, state says 305")
s = state(305.0, 66.36, 305.0)
sync(s, 100.0, unreadable=True)
assert s["cash"] == 66.36 and s["allocation"] == 305.0, s
print(f"  cash untouched at ${s['cash']:.2f}, allocation still "
      f"${s['allocation']:.0f}"); ok += 1

print("\na real raise, 100 -> 305")
s = state(100.0, 40.0, 100.0)
moved = sync(s, 305.0, unreadable=False)
assert s["cash"] == 245.0 and s["contributed"] == 305.0, s
print(f"  {moved:+.2f} into cash, contributed now ${s['contributed']:.0f}")
ok += 1

print("\na cut deeper than the cash available: 305 -> 50 with $20 cash")
s = state(305.0, 20.0, 305.0, positions=250.0)
moved = sync(s, 50.0, unreadable=False)
assert s["cash"] == 0.0 and s["contributed"] == 285.0, s
print(f"  took the {moved:+.2f} that existed; contributed dropped to "
      f"${s['contributed']:.0f} to match")
assert abs(engine.books_drift(s)) < 0.01, engine.books_drift(s)
print(f"  books drift {engine.books_drift(s):+.4f} — nothing vanished")
ok += 1

print("\nan unreadable settings file applies nothing at all")
cconfig.SETTINGS_FILE = "/nonexistent/dir/settings.json"
live, unreadable = csettings.apply()
assert live == {} and unreadable is False   # missing file is not a failure
print("  missing file -> defaults, not a failure"); ok += 1

print(f"\n{ok} checks passed")
