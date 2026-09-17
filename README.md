# sigmatiq-polymarket

Copy-trading bot for Polymarket. Follows the top wallets by profit, mirrors
their buys at a fixed size, sells when they sell, settles positions when
markets resolve, and measures each trader's real win rate from your own
results.

Runs on GitHub Actions. No server.

## Capital rules

```
TOTAL_BALANCE      1000    what's in the wallet
TRADING_ALLOCATION  100    all the bot may ever touch
MAX_POSITION_PCT     5%    per market -> $5 ceiling on a $100 allocation
TRADE_SIZE           $5    target per copied trade
MAX_ALLOCATION_DEPLOYED  100%   the whole allocation can be at work
```

The bot never sees the other $900. Sizing, caps, and "out of cash" all compute
against the allocation only.

## What it won't buy

```
MAX_BUY_PRICE            0.90   above this there's no upside worth the risk
MIN_BUY_PRICE            0.05   below this it's a lottery ticket
MAX_NEW_POSITIONS_PER_RUN   3   one busy trader can't drain the allocation
ONE_POSITION_PER_MARKET  true   no stacking the same bet
MAX_MARKET_DAYS           7     skip markets settling further out than this
MIN_MARKET_HOURS          2     and ones settling too soon to arrive in time
```

The horizon rule is the one that matters most on a small balance. These
traders have millions, so leaving money in a market that resolves in March
costs them nothing. On $100 it is fatal — three long-dated positions and the
allocation is gone while every short market passes you by. The first backtest
showed exactly this: $61 locked in 13 unresolved positions, and 1,716
subsequent trades rejected for want of cash.

The lower bound matters too. Markets settling within an hour are mostly crypto
up/down and live sports, which is where latency-arbitrage bots operate.
Copying one of those twenty seconds late means buying after the move.

The price bounds matter more than they look. A wallet buying at 99¢ wins
almost every time and makes almost nothing when it does, while a single loss
costs the whole stake. High win rate, bad trade.

## A note on win rate

Polymarket's API cannot give you a trader's win rate. `/positions` returns
current holdings, and redeemed winners are deleted from it — so anything
computed from that endpoint counts only the losers nobody bothered clearing.
The leaderboard's win-rate field is null for the same reason.

So traders are ranked on **profit and volume**, which the leaderboard reports
directly and which are real. Win rate is then **measured** by `analyzer.py`
from your own copied trades as they close. That number is trustworthy because
you observed it.

## Scoring

After 8 closed trades with you, a trader who is net negative gets benched and
is no longer copied. Profitable ones stay. Open positions from a benched
trader are still managed to completion.

## Run it

```bash
pip3 install requests
python3 loop.py           # continuous, polls every 30s  <- normal use
python3 run.py            # one pass and exit
python3 run.py analyse    # per-trader report
```

Stop the loop with Ctrl-C.

The first run copies nothing — it marks existing history as seen so weeks of
old trades aren't replayed. Copying starts on the second run.

## Backtest

Before trusting any of this, replay it:

```bash
python3 backtest.py          # ~500 trades per wallet
python3 backtest.py 2000     # deeper history, slower
```

It pulls the same top-20 leaderboard, fetches their past trades, replays them
through the exact rules above, settles each position at the market's real
resolution time, and reports what $100 would have become.

Read the result as a ceiling. It fills at their price while your copy lands
seconds later and worse; the history only reaches back so far; and the wallets
are the ones on the leaderboard today, so anyone who blew up is missing from
the sample. A backtest that looks mediocre here will look worse live.

## Lag

A scheduled job that wakes every N minutes copies a trade up to N minutes after
it happened, and the price has usually moved by then — same bet, worse odds.
Making the scan faster doesn't help; the trade was already stale when the bot
woke up.

So `loop.py` stays alive and polls every 30 seconds instead. On GitHub Actions
one job runs for 5.5 hours, then the schedule starts the next, which keeps
average lag around 15 seconds rather than minutes. Public repos get unlimited
Actions minutes, so this costs nothing.

```
POLL_SECONDS           30    seconds between passes
WINDOW_MINUTES        330    job length; Actions caps a job near 6 hours
COMMIT_EVERY_SECONDS  300    how often state is pushed back to the repo
```

Some lag is unavoidable. Whether it eats the edge is one of the things the
paper period is there to measure.

## Files

```
bot/config.py       every tunable rule
loop.py             continuous runner
bot/polymarket.py   API access, order placement, redemption
bot/copybot.py      the copy loop
bot/resolution.py   settles positions when markets resolve
bot/analyzer.py     measured win rate + benching
bot/state.py        JSON persistence
dashboard/          Vercel page, PIN-gated
data/               state.json, trades.json, traders.json, analysis.json
```

## Dashboard

See `dashboard/README.md`. Static page plus one serverless function; the PIN
is checked server-side so this repo can stay public.

## Going live

Paper mode is the default and needs no wallet. To switch:

1. Add `POLY_PRIVATE_KEY` and `POLY_FUNDER` as GitHub Secrets
2. Change `BOT_MODE: paper` to `live` in `.github/workflows/bot.yml`
3. Approve Polymarket's on-chain spend once, or every order fails

Don't do this until the paper log shows weeks of real closed trades. Three
positions and a 100% win rate is not evidence of anything.
