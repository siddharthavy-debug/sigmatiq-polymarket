# Sigmatiq — Polymarket crypto copy bot

Copies a small set of traders into short crypto UP/DOWN markets (BTC, ETH,
SOL, XRP, DOGE, BNB, HYPE), 5 minutes to 1 hour, on Polymarket's real-time
feed. Paper mode by default: it places no orders and spends nothing.

    crypto/harvest.py   listen to the feed, collect wallets trading crypto
    crypto/weekly.py    score them on a rolling week, print the lists
    crypto/pin.py       choose from a ranking that already ran
    crypto/run.py       the bot: copy, settle, commit
    crypto/engine.py    every trading rule, testable without a network
    dashboard/          the board, PIN-gated

See CRYPTO.md for how to run it.

## Why the traders are picked the way they are

A week of real data, 242 wallets with 50+ settled crypto trades:

* The median wallet's win rate beat the price it paid by **+0.20%**. These
  markets are priced well and there is very little edge to borrow.
* High win rates are worthless on their own. One wallet won **98.6%** of 560
  trades and finished **-$223**, because at 99c a win pays a penny and a loss
  costs 99. Another won 93.8% of 1,005 trades for a 0.4% return.
* The money was at **45-55c**: 126 wallets there, 61% profitable, +$30,517
  between them. Above 85c: ten wallets, six of them "winning" most trades,
  -$4,696 overall.
* Fourteen wallets cleared a z-score of +2 — results beating the prices they
  paid by more than luck explains. Chance alone across 242 would produce about
  six, so roughly half are real and there is no way to tell which. Hence
  following several rather than betting on one.

So the bot ranks on **win rate minus the price paid**, requires statistical
significance, and buys only between 40c and 60c.

## The limits

2% of the pot per trade, compounding. 20% of the pot at work at once. It stops
opening new positions if the day is down 6%. One copy per trader per market —
their second buy chases a price their edge no longer covers.

## What the books check means

Every pass compares equity against contributed capital plus realised P&L. They
must be equal. If a warning appears, the reported P&L is wrong and should not
be trusted — that check exists because a settings file that failed to parse
once silently destroyed real balance in an earlier version.
