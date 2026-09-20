"""
Settings for the crypto engine.

Separate from the sports bot on purpose: different markets, different risk,
different money. Nothing here touches bot/config.py, and the two engines keep
their own state files, so one cannot corrupt the other.

Every value is overridable from the dashboard at decision time — not read once
at startup. That distinction cost us real money last week.
"""
import os


def _f(name, default):
    return float(os.getenv(name, default))


def _i(name, default):
    return int(os.getenv(name, default))


MODE = os.getenv("CRYPTO_MODE", "paper").lower()

# ---------------------------------------------------------------- the money
TOTAL_BALANCE      = _f("CRYPTO_TOTAL_BALANCE", "1000")
TRADING_ALLOCATION = _f("CRYPTO_ALLOCATION", "300")

# Size compounds: 2% of what the pot is worth NOW, so it grows with winnings
# and shrinks after losses without anyone touching a dial.
STAKE_PCT     = _f("CRYPTO_STAKE_PCT", "0.02")
MIN_STAKE_USD = _f("CRYPTO_MIN_STAKE", "1.00")

# ------------------------------------------------------------------- limits
# 2% per trade caps one trade. This caps all of them at once — without it,
# twenty open positions is 40% of the pot riding on the same afternoon.
MAX_DEPLOYED_PCT = _f("CRYPTO_MAX_DEPLOYED", "0.20")

# The way these accounts die is not one bad trade, it is a bad run the bot
# keeps feeding. Down this much on the day and it stops opening new positions.
DAILY_STOP_PCT = _f("CRYPTO_DAILY_STOP", "0.06")

MAX_OPEN_POSITIONS = _i("CRYPTO_MAX_OPEN", "15")

# One copy per trader per market. Their second and third buys are chasing a
# price their edge no longer covers, and repeated buys within seconds is the
# market-maker signature.
ONE_COPY_PER_MARKET_PER_TRADER = True

# ------------------------------------------------------------------ markets
MIN_HORIZON_SECONDS = _i("CRYPTO_MIN_HORIZON", "300")      # 5 minutes
MAX_HORIZON_SECONDS = _i("CRYPTO_MAX_HORIZON", "3600")     # 1 hour

# At 90c a win pays 11c and a loss costs 90c. Below 5c the odds are lottery
# tickets. The band in between is where wins can exceed losses.
MAX_BUY_PRICE = _f("CRYPTO_MAX_PRICE", "0.60")
MIN_BUY_PRICE = _f("CRYPTO_MIN_PRICE", "0.40")

# How stale a signal can be before we refuse it. The feed lands in about a
# second; if we are much further behind than that, the price has moved and we
# would be buying what they already pushed up.
MAX_SIGNAL_AGE_SECONDS = _f("CRYPTO_MAX_SIGNAL_AGE", "8")

# Ignore their dust. A trader risking $2 is not expressing a view.
MIN_THEIR_TRADE_USD = _f("CRYPTO_MIN_THEIR_USD", "20")

PAUSED = os.getenv("CRYPTO_PAUSED", "").lower() in ("1", "true", "yes")

STATE_FILE   = os.path.join("data", "crypto_state.json")
TRADES_FILE  = os.path.join("data", "crypto_trades.json")
PINNED_FILE  = os.path.join("data", "crypto_traders.json")
SETTINGS_FILE = os.path.join("data", "crypto_settings.json")

MAX_TRADE_LOG = _i("CRYPTO_MAX_TRADE_LOG", "4000")
