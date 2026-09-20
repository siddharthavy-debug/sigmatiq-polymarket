#!/usr/bin/env python3
"""
pin.py — choose the traders from a ranking that already ran.

crypto_weekly.json holds every wallet the last review measured, so changing
the thresholds does not mean sitting through another half-hour of API calls.
This re-applies them to that file and writes the bot's trader list.

    python3 -m crypto.pin                 # current thresholds, show only
    python3 -m crypto.pin --write         # write data/crypto_traders.json
    MARGIN_BAR=0.02 python3 -m crypto.pin # try a looser bar first
"""
import json
import os
import sys
from datetime import datetime, timezone

OUT = "crypto_weekly.json"

# Set from the first real run over 242 wallets, not guessed:
#   median margin was +0.20% — these markets are priced well
#   only one wallet reached +8%, which is why the first list came back empty
#   at +3% there are 22 wallets and 17 of them made money
#   fourteen cleared z=+2; chance alone would give about six, so take a dozen
#   45-55c held 126 wallets, 61% profitable, +$30,517 between them
#   above 85c: ten wallets, six "winning" most trades, -$4,696 overall
MARGIN_BAR     = float(os.getenv("MARGIN_BAR", "0.03"))
MIN_Z          = float(os.getenv("MIN_Z", "2.0"))
PIN_MIN_ENTRY  = float(os.getenv("PIN_MIN_ENTRY", "0.35"))
PIN_MAX_ENTRY  = float(os.getenv("PIN_MAX_ENTRY", "0.65"))
PIN_MIN_TRADES = int(os.getenv("PIN_MIN_TRADES", "300"))
TOP_N          = int(os.getenv("TOP_N", "12"))


def main():
    write = "--write" in sys.argv
    try:
        data = json.load(open(OUT))
    except FileNotFoundError:
        print(f"No {OUT}. Run:  python3 -m crypto.weekly 7")
        return 1

    everyone = data.get("all", [])
    print(f"Choosing from {len(everyone)} measured wallets\n")
    print(f"  margin  >= {MARGIN_BAR:.0%}   (they beat the price they paid)")
    print(f"  z       >= +{MIN_Z:g}    (by more than luck explains)")
    print(f"  entries    {PIN_MIN_ENTRY*100:.0f}c-{PIN_MAX_ENTRY*100:.0f}c")
    print(f"  trades  >= {PIN_MIN_TRADES}")
    print(f"  and up on the week\n")

    chosen = [m for m in everyone
              if m.get("margin", 0) >= MARGIN_BAR
              and m.get("edge_z", 0) >= MIN_Z
              and m.get("profit", 0) > 0
              and PIN_MIN_ENTRY <= m.get("avg_entry", 0) <= PIN_MAX_ENTRY
              and m.get("trades", 0) >= PIN_MIN_TRADES]
    chosen.sort(key=lambda m: -m["edge_z"])
    chosen = chosen[:TOP_N]

    if not chosen:
        print("Nobody qualifies. Loosen a threshold or wait for fresh data.")
        return 1

    print(f"{'#':>2} {'trader':<24}{'trades':>7}{'won':>7}{'entry':>6}"
          f"{'margin':>8}{'z':>6}{'profit':>10}")
    print("-" * 71)
    for i, m in enumerate(chosen, 1):
        print(f"{i:>2} {(m['name'] or '')[:23]:<24}{m['trades']:>7}"
              f"{m['win_rate']*100:>6.1f}%{m['avg_entry']*100:>5.0f}c"
              f"{m['margin']*100:>+7.1f}%{m['edge_z']:>+6.1f}"
              f"{m['profit']:>+10,.0f}")

    print("\nwallets:")
    for m in chosen:
        print(f"  {m['wallet']}  {m['name']}")

    if not write:
        print("\nAdd --write to pin these.")
        return 0

    book = {
        "pinned": True,
        "chosen_by": {"margin": MARGIN_BAR, "min_z": MIN_Z,
                      "entry": [PIN_MIN_ENTRY, PIN_MAX_ENTRY],
                      "min_trades": PIN_MIN_TRADES},
        "updated": datetime.now(timezone.utc).isoformat(),
        "traders": [{
            "wallet": m["wallet"], "name": m["name"],
            "win_rate": round(m["win_rate"], 4),
            "avg_entry": round(m["avg_entry"], 4),
            "margin": round(m["margin"], 4),
            "edge_z": round(m["edge_z"], 2),
            "profit": round(m["profit"], 2),
            "trades": m["trades"],
        } for m in chosen],
    }
    os.makedirs("data", exist_ok=True)
    with open("data/crypto_traders.json", "w") as fh:
        json.dump(book, fh, indent=1)
    print(f"\nPinned {len(chosen)} traders -> data/crypto_traders.json")
    print("Commit and push, then start the crypto-bot workflow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
