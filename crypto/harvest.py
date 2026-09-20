#!/usr/bin/env python3
"""
harvest.py — build the candidate pool.

The leaderboard is the wrong place to look for crypto traders: it ranks by
total profit, which is dominated by people making large bets on sports and
politics. A wallet quietly grinding 5-minute BTC markets may never appear on
it at all.

So the pool comes from the crypto markets themselves. This listens to the live
feed and writes down every wallet that actually trades crypto UP/DOWN, with how
often and in which coins. Thirty minutes is enough to surface the regulars;
longer catches the selective ones.

Read-only. Places no orders, touches no bot state.

    python3 -m crypto.harvest              # 30 minutes
    python3 -m crypto.harvest 60           # an hour, a wider net
"""
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from . import feed

OUT = "crypto_candidates.json"


async def run(minutes):
    import websockets

    deadline = time.time() + minutes * 60
    seen = feed.Seen()
    wallets = defaultdict(lambda: {
        "name": None, "trades": 0, "coins": defaultdict(int),
        "markets": set(), "buys": 0, "sells": 0,
        "first": None, "last": None, "size_sum": 0.0,
    })
    captured = 0
    attempt = 0

    print(f"harvesting crypto-active wallets for {minutes} minutes")
    print("(this only listens — nothing is traded)\n", flush=True)

    while time.time() < deadline:
        try:
            async with websockets.connect(
                    feed.URL, ping_interval=15, ping_timeout=45,
                    close_timeout=5, max_queue=2048) as ws:
                await ws.send(feed.subscribe_message())
                attempt = 0
                last_report = time.time()

                while time.time() < deadline:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(deadline - time.time(), 1))
                    for t in feed.parse(raw):
                        if not seen.is_new(t):
                            continue
                        captured += 1
                        w = wallets[t["wallet"]]
                        w["name"] = w["name"] or t.get("name")
                        w["trades"] += 1
                        w["coins"][t["coin"]] += 1
                        w["markets"].add(t["slug"])
                        w["size_sum"] += t["shares"] * t["price"]
                        if (t.get("side") or "BUY").upper() == "SELL":
                            w["sells"] += 1
                        else:
                            w["buys"] += 1
                        if w["first"] is None:
                            w["first"] = t["ts"]
                        w["last"] = t["ts"]

                    if time.time() - last_report > 60:
                        last_report = time.time()
                        mins_left = (deadline - time.time()) / 60
                        print(f"  {datetime.now(timezone.utc):%H:%M:%S}  "
                              f"{captured:,} crypto trades  "
                              f"{len(wallets):,} wallets  "
                              f"{mins_left:.0f} min left", flush=True)

        except asyncio.TimeoutError:
            break
        except Exception as e:
            if time.time() >= deadline:
                break
            wait = min(2 ** attempt, 30)
            attempt += 1
            print(f"  reconnecting in {wait}s ({type(e).__name__})", flush=True)
            await asyncio.sleep(wait)

    return wallets, captured


def main():
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 30
    wallets, captured = asyncio.run(run(minutes))

    out = []
    for wallet, w in wallets.items():
        out.append({
            "wallet": wallet,
            "name": w["name"],
            "trades_seen": w["trades"],
            "markets_seen": len(w["markets"]),
            "buys": w["buys"],
            "sells": w["sells"],
            "avg_size_usd": round(w["size_sum"] / max(w["trades"], 1), 2),
            "coins": dict(sorted(w["coins"].items(),
                                 key=lambda kv: -kv[1])),
        })
    out.sort(key=lambda r: -r["trades_seen"])

    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=1)

    print(f"\n{captured:,} crypto trades from {len(out):,} distinct wallets")
    print(f"written to {OUT}\n")
    print(f"{'wallet':<44} {'name':<22} {'trades':>7} {'mkts':>5}  coins")
    print("-" * 100)
    for r in out[:30]:
        coins = ",".join(list(r["coins"])[:4])
        print(f"{r['wallet']:<44} {(r['name'] or '')[:21]:<22} "
              f"{r['trades_seen']:>7} {r['markets_seen']:>5}  {coins}")
    print(f"\nNext:  python3 -m crypto.rank_history")


if __name__ == "__main__":
    sys.exit(main() or 0)
