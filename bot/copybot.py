"""
Sigmatiq-Polymarket copy bot.

One run: refresh the trader list if stale, poll each trader's recent activity,
copy anything new. Paper mode by default — nothing real is placed.

First run is deliberately a no-op for trading: it marks existing history as
seen so the bot doesn't retroactively copy weeks of old trades.
"""
import time
from datetime import datetime, timezone, timedelta

from . import config, state
from . import polymarket as pm
from . import analyzer
from . import resolution
from . import settings


def _stale(ts, hours):
    if not ts:
        return True
    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - then > timedelta(hours=hours)


def refresh_traders():
    book = state.load_traders()
    benched = set(book.get("benched", []))

    # A pinned list comes from rank_traders.py, which scores wallets on what
    # they actually do in short markets. The daily leaderboard refresh is what
    # picked SDTrading — +$33,913 on the board, -$98,720 in the markets we
    # copy — so when a list is pinned we leave it alone and let the analyzer
    # bench whoever loses real money.
    if book.get("pinned"):
        traders = book.get("traders", [])
        print(f"[traders] pinned: {len(traders)} ranked wallets")
    elif not _stale(book.get("updated"), config.REFRESH_HOURS) and book.get("traders"):
        traders = book["traders"]
        print(f"[traders] cached: {len(traders)}")
    else:
        fetched = pm.fetch_top_traders()
        if not fetched:
            print("[traders] leaderboard empty — reusing previous list")
            traders = book.get("traders", [])
        else:
            traders = fetched
            book["traders"] = traders
            state.save_traders(book)
            print(f"[traders] refreshed: {len(traders)} qualifying wallets")

    active = [t for t in traders if t["wallet"] not in benched]
    if len(active) < len(traders):
        print(f"[traders] {len(traders) - len(active)} benched by analyzer")
    return active


def _key(t):
    return (t.get("transactionHash")
            or f"{t.get('proxyWallet')}_{t.get('timestamp')}_{t.get('asset')}")


def trade_size(s):
    """Fixed size, clamped to 5% of the ALLOCATION (never the full balance)."""
    cap = config.TRADING_ALLOCATION * config.MAX_POSITION_PCT
    return round(min(config.TRADE_SIZE, cap, max(s["cash"], 0)), 2)


def handle_buy(s, t, trader, run_ctx=None, now_ts=None):
    """now_ts lets the backtest judge horizons as of the historical moment
    rather than today, when every past market has already settled."""
    token = t.get("asset")
    price = float(t.get("price") or 0)
    if not token or price <= 0:
        return "skipped", "missing token or price"

    # Skip the extremes: no upside at 0.99, no realistic odds at 0.02.
    if price > config.MAX_BUY_PRICE:
        return "skipped", f"price {price:.3f} above {config.MAX_BUY_PRICE} (no upside)"
    if price < config.MIN_BUY_PRICE:
        return "skipped", f"price {price:.3f} below {config.MIN_BUY_PRICE} (longshot)"

    is_new_market = token not in s["positions"]

    if is_new_market and config.ONE_POSITION_PER_MARKET:
        market = t.get("title") or t.get("slug")
        if market and any(p.get("market") == market for p in s["positions"].values()):
            return "skipped", "already hold this market"

    # Concentration: one wallet shouldn't hold most of the book. The whole
    # point of following 20 traders is not depending on any single one.
    if is_new_market:
        mine = sum(1 for p in s["positions"].values()
                   if p.get("trader_wallet") == trader["wallet"])
        if mine >= config.MAX_POSITIONS_PER_TRADER:
            return "skipped", f"already hold {mine} from this trader"

    # Horizon: skip markets that won't settle soon enough to recycle the
    # capital, and ones settling so soon we'd be arriving after the move.
    if is_new_market and config.MAX_MARKET_DAYS:
        end = pm.fetch_market_end(t.get("slug"))
        if end is not None:
            hours = (end - (now_ts or time.time())) / 3600
            if hours > config.MAX_MARKET_DAYS * 24:
                return "skipped", f"settles in {hours/24:.0f}d, too far out"
            if hours < config.MIN_MARKET_HOURS:
                return "skipped", f"settles in {max(hours,0):.1f}h, too soon"

    # Pacing: don't let one busy trader drain the allocation in a single cycle.
    if run_ctx is not None and is_new_market:
        if run_ctx["new_positions"] >= config.MAX_NEW_POSITIONS_PER_RUN:
            return "skipped", "per-run new position cap reached"

    deployed = sum(p["cost"] for p in s["positions"].values())
    ceiling = config.TRADING_ALLOCATION * config.MAX_ALLOCATION_DEPLOYED
    if deployed >= ceiling:
        return "skipped", f"allocation {deployed:.0f}/{ceiling:.0f} deployed, holding reserve"

    # The 5% ceiling is per MARKET, not per order. Without this a repeat buy
    # into a position we already hold stacks cost past the cap — three $3 buys
    # into one market is a $9 bet, not a $3 one.
    held_cost = s["positions"].get(token, {}).get("cost", 0.0)
    per_market_cap = config.TRADING_ALLOCATION * config.MAX_POSITION_PCT
    room = per_market_cap - held_cost
    if room < config.MIN_TRADE_USD:
        return "skipped", f"market already at ${held_cost:.2f} of ${per_market_cap:.2f} cap"

    size = min(trade_size(s), room)
    size = round(size, 2)
    if size < config.MIN_TRADE_USD:
        return "skipped", "no cash left in allocation"
    if len(s["positions"]) >= config.MAX_OPEN_POSITIONS:
        return "skipped", "max open positions"

    if config.MODE == "live":
        ok, detail = pm.place_order(token, "BUY", size_usd=size)
        if not ok:
            return "failed", str(detail)

    shares = size / price
    if is_new_market and run_ctx is not None:
        run_ctx["new_positions"] += 1
    pos = s["positions"].setdefault(token, {
        "market": t.get("title") or t.get("slug") or token[:16],
        "slug": t.get("slug"),
        "outcome": t.get("outcome"),
        "shares": 0.0,
        "cost": 0.0,
        "trader": trader["name"],
        "trader_wallet": trader["wallet"],
        "opened": state.now(),
    })
    pos["shares"] += shares
    pos["cost"] += size
    s["cash"] -= size
    s["trades_executed"] += 1
    return "executed", {"shares": round(shares, 4), "price": price, "size": size}


def handle_sell(s, t, trader):
    token = t.get("asset")
    pos = s["positions"].get(token)
    if not pos:
        return "skipped", "we don't hold this"

    price = float(t.get("price") or 0)
    if price <= 0:
        return "skipped", "missing price"

    frac = 1.0
    if config.PROPORTIONAL_SELLS:
        sold = float(t.get("size") or 0)
        left = float(t.get("remaining") or 0)
        prev = sold + left
        if prev > 0:
            frac = max(0.0, min(1.0, sold / prev))

    shares_out = pos["shares"] * frac
    if shares_out <= 0:
        return "skipped", "nothing to sell"

    if config.MODE == "live":
        ok, detail = pm.place_order(token, "SELL", shares=shares_out)
        if not ok:
            return "failed", str(detail)

    proceeds = shares_out * price
    cost_out = pos["cost"] * frac
    pnl = proceeds - cost_out

    pos["shares"] -= shares_out
    pos["cost"] -= cost_out
    s["cash"] += proceeds
    s["realized_pnl"] += pnl
    s["trades_executed"] += 1
    if pnl >= 0:
        s["wins"] += 1
    else:
        s["losses"] += 1

    if pos["shares"] <= 1e-6:
        del s["positions"][token]

    return "executed", {
        "shares": round(shares_out, 4), "price": price,
        "proceeds": round(proceeds, 2), "pnl": round(pnl, 2),
        "fraction": round(frac, 3),
    }


def run():
    # Dashboard-editable settings override config for this pass.
    overrides, paused = settings.apply()
    if overrides:
        shown = {k: v for k, v in overrides.items() if k != "mode_downgraded"}
        if shown:
            print(f"[settings] {shown}")
    if overrides.get("mode_downgraded"):
        print("[settings] live requested but POLY_PRIVATE_KEY is not set "
              "— staying on paper")
    if paused:
        print("[paused] bot is paused from the dashboard; no trading this pass")
        st = state.load_state()
        st["paused"] = True
        state.save_state(st)
        return

    s = state.load_state()
    s["paused"] = False
    s["mode"] = config.MODE

    # Changing the allocation has to move real cash, not just the number we
    # measure against. Raise it from 100 to 305 and the bot should have $205
    # more to spend; without this it would keep trading on the old cash and
    # report itself down 70% against a target it was never given.
    previous = s.get("allocation", config.TRADING_ALLOCATION)
    s.setdefault("contributed", previous)

    if settings.LOAD_FAILED:
        # We could not read the settings file this pass, so config is holding
        # the workflow's defaults rather than what the dashboard says. Acting
        # on that would move real cash against a number the user never set.
        config.TRADING_ALLOCATION = previous
        print("[allocation] settings unreadable this pass — leaving it alone")
    elif abs(previous - config.TRADING_ALLOCATION) > 1e-9:
        delta = config.TRADING_ALLOCATION - previous
        cash = s.get("cash", 0.0)
        # Taking out more than is sitting in cash cannot silently vanish:
        # remove what is actually there and keep the books balanced by
        # adjusting contributed capital to match.
        actual = delta if delta >= 0 else -min(-delta, cash)
        s["cash"] = cash + actual
        s["contributed"] = s.get("contributed", previous) + actual
        note = "" if abs(actual - delta) < 1e-9 else \
            f" (only {actual:+.2f} — the rest is tied up in open positions)"
        print(f"[allocation] {previous:.2f} -> {config.TRADING_ALLOCATION:.2f} "
              f"({actual:+.2f} cash){note}")
    s["allocation"] = config.TRADING_ALLOCATION
    s["total_balance"] = config.TOTAL_BALANCE

    print(f"=== sigmatiq-polymarket | mode={config.MODE} | "
          f"allocation ${config.TRADING_ALLOCATION:.0f} of ${config.TOTAL_BALANCE:.0f} ===")

    settled = 0
    if s["positions"]:
        settled = resolution.check_and_settle(s, verbose=True)
        if settled:
            print(f"[resolve] settled {settled} position(s)")

    traders = refresh_traders()
    if not traders:
        print("no traders available — nothing to do")
        state.save_state(s)
        return

    first_run = not s.get("initialised")
    seen = set(s["seen_trades"])
    fresh = 0
    run_ctx = {"new_positions": 0}
    skip_tally = {}
    executed_this_pass = 0

    for trader in traders:
        for t in pm.fetch_trader_activity(trader["wallet"]):
            k = _key(t)
            if k in seen:
                continue
            seen.add(k)
            s["seen_trades"].append(k)
            fresh += 1

            if first_run:
                continue    # mark history seen, copy nothing

            side = (t.get("side") or "").upper()
            their_usd = float(t.get("usdcSize") or t.get("size") or 0)
            if their_usd < config.MIN_TRADE_USD:
                s["skipped"] += 1
                continue

            if side == "BUY" and config.MIRROR_BUYS:
                status, detail = handle_buy(s, t, trader, run_ctx)
            elif side == "SELL":
                status, detail = handle_sell(s, t, trader)
            else:
                continue

            if status == "skipped":
                s["skipped"] += 1
                reason = detail if isinstance(detail, str) else "other"
                # collapse the price reasons so they group
                if "above" in reason:
                    reason = "price too high"
                elif "below" in reason:
                    reason = "price too low"
                elif "from this trader" in reason:
                    reason = "per-trader cap"
                elif "too far out" in reason:
                    reason = "settles too far out"
                elif "too soon" in reason:
                    reason = "settles too soon"
                skip_tally[reason] = skip_tally.get(reason, 0) + 1
            elif status == "failed":
                s["failed"] += 1

            state.append_trade({
                "status": status,
                "side": side,
                "mode": config.MODE,
                "trader": trader["name"],
                "trader_wallet": trader["wallet"],
                "market": t.get("title") or t.get("slug"),
                "outcome": t.get("outcome"),
                "their_usd": their_usd,
                "detail": detail,
            })
            if status != "skipped":
                executed_this_pass += 1
                print(f"  {status:9} {side:4} {trader['name'][:18]:18} {detail}")

    if first_run:
        s["initialised"] = True
        print(f"[init] {fresh} existing trades marked as seen. "
              f"Copying begins on the next run.")

    if skip_tally:
        summary = ", ".join(f"{n} {why}" for why, n in
                            sorted(skip_tally.items(), key=lambda x: -x[1]))
        print(f"  skipped: {summary}")

    invested = sum(p["cost"] for p in s["positions"].values())
    equity = s["cash"] + invested
    eventful = executed_this_pass or settled

    # Equity must equal what was put in plus what was made. When it doesn't,
    # the P&L being reported is wrong and everything built on it is too — so
    # say so on the spot rather than letting it be discovered a week later.
    contributed = s.get("contributed", s.get("allocation", 0.0))
    drift = equity - (contributed + s["realized_pnl"])
    if abs(drift) > 0.05:
        print(f"  ! books out by ${drift:+.2f} "
              f"(equity ${equity:.2f} vs contributed ${contributed:.2f} "
              f"{s['realized_pnl']:+.2f} realised)")

    if eventful:
        print(f"\ncash ${s['cash']:.2f} | in positions ${invested:.2f} | "
              f"equity ${equity:.2f} | realised ${s['realized_pnl']:+.2f}")
        print(f"open {len(s['positions'])} | executed {s['trades_executed']} | "
              f"skipped {s['skipped']} | failed {s['failed']}")
        print(f"untouched reserve: "
              f"${config.TOTAL_BALANCE - config.TRADING_ALLOCATION:.2f}")
    else:
        print(f"  nothing new · equity ${equity:.2f} · "
              f"{len(s['positions'])} open")

    state.save_state(s)
    # The full trader table every 30 seconds is noise. Show it only when
    # something actually changed.
    analyzer.analyse(verbose=eventful)


if __name__ == "__main__":
    run()
