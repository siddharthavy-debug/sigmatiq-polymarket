# The crypto engine

Three commands, in order. Nothing here touches the sports bot — different
state files, different settings, different money.

## 1. Find the traders (terminal, ~50 minutes, read-only)

```
python3 -m crypto.harvest 30      # listen 30 min, collect crypto-active wallets
python3 -m crypto.weekly 7        # rank them on the last 7 days, print only
```

`weekly` prints three tables over the same wallets:

| list | rule |
|------|------|
| **A**  | made money on ≥70% of trades **and** finished the week up |
| **A'** | the same 70% bar with the profit condition removed |
| **B**  | win rate at least 8 points above the price they paid, and up on the week |

A' exists to show what the win-rate rule lets in on its own. If it is full of
wallets winning 80% at 84¢ and finishing down, the table has settled the
argument. Nothing is pinned until you add `--pin` (list B) or `--pin=a`.

## 2. Run the bot (paper)

```
python3 -m crypto.weekly 7 --pin      # commit a trader list
git add data/ && git commit -m "crypto traders" && git push
```

Then Actions → **crypto-bot** → Run workflow. It holds the feed open for 5.5
hours and the schedule starts the next one.

## 3. Watch it

The dashboard has a **Crypto** tab. Same PIN.

---

## The rules it trades by

| | default | why |
|---|---|---|
| per trade | 2% of the pot | compounds as the balance moves, no dial to touch |
| most at work at once | 20% | 2% each caps one trade; this caps all of them |
| daily stop | 6% | these accounts die from bad runs, not bad trades |
| markets | 5m – 1h | short enough to cycle capital, long enough that a 2s lag isn't the whole edge |
| price | 15¢ – 65¢ | above 65¢ a win pays less than a loss |
| per trader per market | one copy | their second buy chases a price their edge no longer covers |
| stale signals | ignored after 8s | a late signal is a price that already moved |
| their dust | ignored under $20 | a trader risking $5 isn't expressing a view |

All adjustable from the dashboard, all read at decision time — change a number
and the next trade uses it. After saving, the page reads the values back from
what the bot will actually use, so a change that didn't take is visible
immediately.

## Going live

Not yet. When the paper results earn it:

1. Make the repo **private** — a live wallet key should not sit in a public
   repo's settings.
2. Deposit USDC through Polymarket's own app and place one small trade by
   hand. That handles the on-chain approvals.
3. Add `POLY_PRIVATE_KEY` and `POLY_FUNDER` to GitHub Secrets.
4. Switch the dashboard to Live (it asks twice).

Without the key the bot stays on paper no matter what the dashboard says.

## What the books check means

Every pass compares equity against contributed capital plus realised P&L. They
must be equal. If a line appears saying the books are out, the reported P&L is
wrong and should not be trusted until it is fixed — that check exists because a
settings file that failed to parse once silently destroyed real balance.
