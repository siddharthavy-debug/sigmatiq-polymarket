#!/usr/bin/env python3
"""
longshot.py -- is Polymarket's pricing calibrated?

The claim worth testing: cheap outcomes are overpriced and expensive ones
underpriced (the "longshot bias"). If a 90c favourite wins 95% of the time,
buying favourites has positive expected value regardless of anything we know
about the subject.

Method, and the trap it avoids: a settled market's outcomePrices are the
SETTLEMENT (1 and 0), not what anyone paid. Calibration measured against those
is circular and always perfect. So for every settled market this pulls the CLOB
price history and takes the price at a fixed lead time before close -- what the
market actually believed while it was still a question -- and checks it against
what happened.

Read-only. Places no orders and touches no bot state.

    python3 -m research.longshot                 # default 1500 markets
    python3 -m research.longshot 4000 24         # 4000 markets, 24h lead
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from crypto import api

OUT = "research/longshot_result.json"

# Markets thinner than this have prices nobody traded on.
MIN_VOLUME = 500.0

# Buckets on the price paid.
EDGES = [0.0, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50,
         0.60, 0.70, 0.80, 0.90, 0.95, 1.0]


def bucket(p):
    for i in range(len(EDGES) - 1):
        if EDGES[i] <= p < EDGES[i + 1]:
            return i
    return len(EDGES) - 2


def settled_markets(limit):
    """Closed markets, newest first, paged."""
    out, offset, page = [], 0, 500
    while len(out) < limit:
        rows = api._rows(api._get(
            f"{api.GAMMA_API}/markets",
            {"closed": "true", "limit": page, "offset": offset,
             "order": "endDate", "ascending": "false"}))
        if not rows:
            break
        out.extend(rows)
        offset += page
        print(f"  fetched {len(out)}")
        time.sleep(0.25)
    return out[:limit]


def price_before(token_id, close_ts, lead_hours):
    """What this token traded at `lead_hours` before the market closed."""
    want = close_ts - lead_hours * 3600
    data = api._get(f"{api.CLOB_API}/prices-history",
                    {"market": token_id,
                     "startTs": int(want - 3600),
                     "endTs": int(want + 900),
                     "fidelity": 10})
    pts = (data or {}).get("history") or []
    if not pts:
        return None
    # nearest point at or before the target
    best = None
    for p in pts:
        t = p.get("t")
        if t is None or t > want + 900:
            continue
        if best is None or abs(t - want) < abs(best.get("t", 0) - want):
            best = p
    if best is None:
        return None
    try:
        return float(best["p"])
    except (KeyError, TypeError, ValueError):
        return None


def run(limit, lead_hours):
    print(f"pulling up to {limit} settled markets, "
          f"price taken {lead_hours}h before close")
    markets = settled_markets(limit)
    print(f"{len(markets)} settled markets\n")

    rows = []
    skipped = defaultdict(int)

    for i, m in enumerate(markets):
        if i % 100 == 0 and i:
            print(f"  scored {i}/{len(markets)}  usable={len(rows)}")

        vol = float(m.get("volumeNum") or 0)
        if vol < MIN_VOLUME:
            skipped["thin"] += 1
            continue

        prices = api._maybe_json(m.get("outcomePrices"))
        tokens = api._maybe_json(m.get("clobTokenIds"))
        outcomes = api._maybe_json(m.get("outcomes"))
        if not (prices and tokens) or len(prices) != len(tokens):
            skipped["malformed"] += 1
            continue

        try:
            settle = [float(x) for x in prices]
        except (TypeError, ValueError):
            skipped["malformed"] += 1
            continue

        # A real resolution is one side at 1 and the rest at 0.
        if abs(sum(settle) - 1.0) > 0.01 or max(settle) < 0.99:
            skipped["unresolved"] += 1
            continue

        close_ts = api._parse_ts(m.get("endDate"))
        if not close_ts:
            skipped["no_date"] += 1
            continue

        for idx, tok in enumerate(tokens):
            p = price_before(tok, close_ts, lead_hours)
            if p is None or not (0.01 <= p <= 0.99):
                skipped["no_history"] += 1
                continue
            rows.append({
                "slug": m.get("slug"),
                "outcome": outcomes[idx] if outcomes and idx < len(outcomes) else str(idx),
                "price": round(p, 4),
                "won": 1 if settle[idx] > 0.5 else 0,
                "volume": vol,
                "end": m.get("endDate"),
            })
        time.sleep(0.05)

    print(f"\n{len(rows)} priced outcomes  skipped={dict(skipped)}\n")
    report(rows)

    os.makedirs("research", exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(),
            "lead_hours": lead_hours,
            "min_volume": MIN_VOLUME,
            "n": len(rows),
            "rows": rows,
        }, f, indent=2)
    print(f"wrote {OUT}")


def report(rows):
    if not rows:
        print("nothing to report")
        return

    buckets = defaultdict(lambda: {"n": 0, "wins": 0, "paid": 0.0})
    for r in rows:
        b = buckets[bucket(r["price"])]
        b["n"] += 1
        b["wins"] += r["won"]
        b["paid"] += r["price"]

    print("price paid        n     implied   actual    edge      EV per $1")
    print("-" * 68)
    total_ev = 0.0
    for i in sorted(buckets):
        b = buckets[i]
        if not b["n"]:
            continue
        implied = b["paid"] / b["n"]
        actual = b["wins"] / b["n"]
        edge = actual - implied
        # buy at the average price, collect $1 on a win
        ev = (actual / implied - 1.0) if implied else 0.0
        total_ev += edge * b["n"]
        flag = ""
        if b["n"] >= 30:
            # rough 2-sigma band on the win rate
            se = (implied * (1 - implied) / b["n"]) ** 0.5
            if abs(edge) > 2 * se:
                flag = "  <-- significant"
        print(f"{EDGES[i]*100:>3.0f}-{EDGES[i+1]*100:<3.0f}c  {b['n']:>7}   "
              f"{implied*100:>6.1f}%  {actual*100:>6.1f}%  "
              f"{edge*100:>+6.1f}pp  {ev*100:>+7.1f}%{flag}")

    n = len(rows)
    paid = sum(r["price"] for r in rows)
    wins = sum(r["won"] for r in rows)
    print("-" * 68)
    print(f"overall   n={n}  paid={paid:.1f}  won={wins}  "
          f"net={wins - paid:+.1f}  ({(wins - paid) / paid * 100:+.2f}% on turnover)")
    print("\nA bucket only matters if it is marked significant AND has enough")
    print("markets that we could actually have traded it repeatedly.")


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    lead = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    run(limit, lead)
