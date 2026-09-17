"""
Analyzer — the real win rate, measured from OUR copied trades.

Polymarket's API can't tell us a trader's win rate (redeemed winners are
deleted from their positions, so anything computed from it is wrong). So we
measure it ourselves: every position we copy and later close is a real,
observed outcome. After enough closed trades this number is trustworthy in a
way their public stats never were.

Also decides who gets benched. A trader with enough closed trades who is net
negative for us stops being copied.
"""
from collections import defaultdict

from . import config, state


def analyse(verbose=True):
    trades = state.load_trades()
    per = defaultdict(lambda: {
        "wallet": "", "copied": 0, "closed": 0, "wins": 0, "losses": 0,
        "pnl": 0.0, "skipped": 0, "failed": 0,
    })

    for t in trades:
        name = t.get("trader", "unknown")
        row = per[name]
        row["wallet"] = t.get("trader_wallet", row["wallet"])

        status = t.get("status")
        if status == "skipped":
            row["skipped"] += 1
            continue
        if status == "failed":
            row["failed"] += 1
            continue
        if status != "executed":
            continue

        detail = t.get("detail") or {}
        if t.get("side") == "BUY":
            row["copied"] += 1
        elif t.get("side") == "SELL" and isinstance(detail, dict):
            pnl = float(detail.get("pnl", 0) or 0)
            row["closed"] += 1
            row["pnl"] += pnl
            if pnl >= 0:
                row["wins"] += 1
            else:
                row["losses"] += 1

    out = []
    benched = []
    for name, r in per.items():
        wr = (r["wins"] / r["closed"] * 100) if r["closed"] else None
        bench = (r["closed"] >= config.SCORING_MIN_CLOSED
                 and r["pnl"] < config.BENCH_IF_PNL_BELOW)
        if bench and r["wallet"]:
            benched.append(r["wallet"])
        out.append({
            "trader": name,
            "wallet": r["wallet"],
            "copied": r["copied"],
            "closed": r["closed"],
            "wins": r["wins"],
            "losses": r["losses"],
            "win_rate": round(wr, 1) if wr is not None else None,
            "pnl": round(r["pnl"], 2),
            "pnl_per_closed": round(r["pnl"] / r["closed"], 2) if r["closed"] else None,
            "skipped": r["skipped"],
            "failed": r["failed"],
            "benched": bench,
        })

    out.sort(key=lambda x: x["pnl"], reverse=True)

    state.save_analysis({
        "updated": state.now(),
        "by_trader": out,
        "benched_wallets": benched,
        "total_logged": len(trades),
    })

    # keep the benched list on the traders file so the bot reads it next run
    book = state.load_traders()
    book["benched"] = benched
    state.save_traders(book)

    if verbose:
        print(f"\n{'TRADER':22}{'COPIED':>7}{'CLOSED':>7}{'WIN%':>7}{'PNL':>10}  ")
        print("-" * 60)
        for r in out:
            wr = f"{r['win_rate']}%" if r["win_rate"] is not None else "-"
            flag = "  BENCHED" if r["benched"] else ""
            print(f"{r['trader'][:21]:22}{r['copied']:>7}{r['closed']:>7}"
                  f"{wr:>7}{r['pnl']:>10.2f}{flag}")
        if not out:
            print("(nothing logged yet)")
        if benched:
            print(f"\n{len(benched)} trader(s) benched — no longer copied.")

    return out


if __name__ == "__main__":
    analyse()
