"""
Resolution / redemption.

A position only closes when the trader we copied sells it. But plenty of
traders hold to resolution, and those positions would otherwise sit open
forever — cash locked, P&L never booked, win rate never populated.

This module checks every open position against the market's current state and
settles the ones that have resolved:

    winning outcome  -> price goes to 1.0, we collect shares * 1.0
    losing outcome   -> price goes to 0.0, the position is written off

In live mode the winnings also have to be redeemed on-chain, otherwise the
USDC never actually lands back in the wallet.
"""
from . import config, state
from . import polymarket as pm


RESOLVED_HIGH = 0.995
RESOLVED_LOW = 0.005


def check_and_settle(s, verbose=True):
    """Settle every open position whose market has resolved. Returns count."""
    if not s["positions"]:
        return 0

    settled = 0
    for token in list(s["positions"].keys()):
        pos = s["positions"][token]

        price = pm.fetch_price(token, "SELL")
        if price is None:
            price = pm.fetch_price(token, "BUY")
        if price is None:
            continue

        won = price >= RESOLVED_HIGH
        lost = price <= RESOLVED_LOW
        if not (won or lost):
            continue        # still trading

        shares = pos["shares"]
        cost = pos["cost"]
        proceeds = shares * (1.0 if won else 0.0)
        pnl = proceeds - cost

        if config.MODE == "live" and won:
            ok, detail = pm.redeem(token)
            if not ok and verbose:
                print(f"  ! redeem failed for {pos['market'][:30]}: {detail}")

        s["cash"] += proceeds
        s["realized_pnl"] += pnl
        if pnl >= 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        del s["positions"][token]
        settled += 1

        state.append_trade({
            "status": "executed",
            "side": "SELL",
            "close_reason": "resolved",
            "mode": config.MODE,
            "trader": pos["trader"],
            "trader_wallet": pos.get("trader_wallet", ""),
            "market": pos["market"],
            "outcome": pos.get("outcome"),
            "their_usd": 0,
            "detail": {
                "shares": round(shares, 4),
                "price": 1.0 if won else 0.0,
                "proceeds": round(proceeds, 2),
                "pnl": round(pnl, 2),
                "result": "WON" if won else "LOST",
            },
        })

        if verbose:
            tag = "WON " if won else "LOST"
            print(f"  resolved  {tag} {pos['trader'][:16]:16} "
                  f"{str(pos['market'])[:34]:34} {pnl:+.2f}")

    return settled
