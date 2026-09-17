"""
Sigmatiq-Polymarket — all tunable rules live here.

Nothing else in the bot hardcodes a number. Change it here, not in the logic.
"""
import os

# ---------------------------------------------------------------- MODE
# "paper" = simulate only. No wallet, no keys, no real orders. Default.
# "live"  = place real orders. Requires POLY_PRIVATE_KEY in the environment.
MODE = os.getenv("BOT_MODE", "paper").lower()

# ---------------------------------------------------------------- CAPITAL
# TOTAL_BALANCE is what's in the wallet. TRADING_ALLOCATION is all the bot is
# ever allowed to touch. The difference is invisible to the bot — it sizes,
# caps and runs out of cash against the allocation only.
TOTAL_BALANCE      = float(os.getenv("TOTAL_BALANCE", "1000"))
TRADING_ALLOCATION = float(os.getenv("TRADING_ALLOCATION", "100"))

MAX_POSITION_PCT = 0.05     # 5% of allocation per market -> $5 on $100
TRADE_SIZE       = 5.00     # target size per copied trade, USD
MIN_TRADE_USD    = 1.00     # below this we don't bother

# ---------------------------------------------------------------- PRICE BOUNDS
# Don't buy near the extremes. At 0.99 the most you can gain is 1% while the
# most you can lose is 100% — a great win rate and a terrible trade. At 0.02
# it's a lottery ticket. Both ends get skipped.
MAX_BUY_PRICE = 0.90
MIN_BUY_PRICE = 0.05

# ---------------------------------------------------------------- PACING
# These traders are high-frequency. Without caps the allocation is fully
# deployed within a couple of cycles and then everything is skipped for days
# until markets resolve.
MAX_NEW_POSITIONS_PER_RUN = 3     # new markets entered per cycle
MAX_POSITIONS_PER_TRADER  = 4     # stops one busy wallet owning your whole book
ONE_POSITION_PER_MARKET   = True  # don't stack the same market repeatedly
MAX_ALLOCATION_DEPLOYED   = 1.00  # deploy the whole allocation

# ---------------------------------------------------------------- HORIZON
# Only copy markets that settle within this many days.
#
# These traders have millions, so parking money in a market that resolves in
# March costs them nothing. On $100 it is fatal: three long-dated positions
# and the allocation is gone while every short market passes you by.
#
# Don't push this too low either. Markets resolving within hours are mostly
# crypto up/down and live sports, which is where latency-arbitrage bots
# operate — copying those 20 seconds late is buying after the move.
MAX_MARKET_DAYS = float(os.getenv("MAX_MARKET_DAYS", "7"))
MIN_MARKET_HOURS = float(os.getenv("MIN_MARKET_HOURS", "2"))

# ---------------------------------------------------------------- TRADERS
TOP_N_TRADERS      = 20
LEADERBOARD_PERIOD = "7d"       # 1d | 7d | 30d | all
REFRESH_HOURS      = 24         # re-pull the list once a day
# Both are env-overridable so you can see how many wallets each filter costs
# you. Loosen them if the bot keeps finding only a handful of traders.
MIN_TRADER_VOLUME = float(os.getenv("MIN_TRADER_VOLUME", "1000"))
# Top 20 by leaderboard profit, no profit floor. The floor was cutting the
# list to 7-11 and starving the bot of traders. The analyzer benches whoever
# loses money on OUR results, which is a better filter than a 7-day number.
MIN_TRADER_PNL    = float(os.getenv("MIN_TRADER_PNL", "0"))

# ---------------------------------------------------------------- SCORING
# After a trader has this many CLOSED copied trades, we judge them on OUR
# results. Net negative -> benched, no longer copied. This is the only
# "learning" in the bot and it runs on measured outcomes, not predictions.
SCORING_MIN_CLOSED = 8
BENCH_IF_PNL_BELOW = 0.0

# ---------------------------------------------------------------- COPY RULES
MIRROR_BUYS        = True
PROPORTIONAL_SELLS = True    # they dump 50% of their stack -> we dump 50% of ours
MAX_OPEN_POSITIONS = 25

# ---------------------------------------------------------------- ENDPOINTS
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ---------------------------------------------------------------- SECRETS
# Live mode only. Set these as GitHub Secrets, never in code.
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
POLY_FUNDER      = os.getenv("POLY_FUNDER", "")

# ---------------------------------------------------------------- FILES
STATE_FILE    = "data/state.json"
TRADES_FILE   = "data/trades.json"
TRADERS_FILE  = "data/traders.json"
ANALYSIS_FILE = "data/analysis.json"
