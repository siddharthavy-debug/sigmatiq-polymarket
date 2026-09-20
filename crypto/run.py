#!/usr/bin/env python3
"""
The crypto copy bot.

Listens to Polymarket's real-time feed, and when one of the pinned traders
buys into a short crypto UP/DOWN market, copies it — subject to every rule in
engine.py. Settles positions as the markets resolve, a few minutes later.

Paper mode places no orders and spends nothing. Live mode needs a wallet key
in the environment and is refused without one.

    python3 -m crypto.run                     # 5.5h window for Actions
    WINDOW_MINUTES=10 python3 -m crypto.run   # short local run

Separate from the sports bot in every way that matters: own state file, own
settings, own trade log. Neither can corrupt the other.
"""
import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import api as pm
from . import cconfig, csettings, cstate, engine, feed, settle

WINDOW_MINUTES = int(os.getenv("WINDOW_MINUTES", "330"))
SETTLE_EVERY = int(os.getenv("SETTLE_EVERY_SECONDS", "60"))
COMMIT_EVERY = int(os.getenv("COMMIT_EVERY_SECONDS", "300"))
IN_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"

RECONNECT_BACKOFF = [1, 2, 5, 10, 20, 30]


def _git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True)


def commit(label):
    if not IN_ACTIONS:
        return False
    branch = os.getenv("GITHUB_REF_NAME") or "main"
    _git("config", "user.name", "cryptobot")
    _git("config", "user.email", "cryptobot@users.noreply.github.com")
    if os.path.isdir(".git/rebase-merge") or os.path.isdir(".git/rebase-apply"):
        _git("rebase", "--abort")
    _git("add", "data/")
    if _git("diff", "--staged", "--quiet").returncode == 0:
        return False
    _git("commit", "-m", f"crypto {label}")
    if _git("push", "origin", f"HEAD:{branch}").returncode == 0:
        return True
    _git("fetch", "origin", branch)
    if _git("rebase", "-X", "theirs", f"origin/{branch}").returncode != 0:
        _git("rebase", "--abort")
        return False
    return _git("push", "origin", f"HEAD:{branch}").returncode == 0


class Bot:
    def __init__(self):
        self.s = cstate.load()
        self.pinned = cstate.load_pinned()
        self.seen = feed.Seen()
        self.pending = {}          # slug -> {"end":, "tokens": {token: ...}}
        self.pending_log = []
        self.skips = {}
        self.copies = 0
        self.settled = 0
        self.reconnects = 0
        self.refresh_settings()

    # -------------------------------------------------------------- config
    def refresh_settings(self):
        """
        Read the dashboard's settings fresh. Called before every decision —
        nothing is cached from startup, so a change takes effect on the very
        next trade rather than the next restart.
        """
        live, unreadable = csettings.apply()
        moved = engine.sync_allocation(self.s, unreadable)
        if unreadable:
            print("  settings unreadable — leaving everything as it is",
                  flush=True)
        elif moved is not None:
            print(f"  allocation -> ${cconfig.TRADING_ALLOCATION:.2f} "
                  f"({moved:+.2f} cash)", flush=True)
        self.s["mode"] = cconfig.MODE
        self.s["total_balance"] = cconfig.TOTAL_BALANCE
        self.s["live_settings"] = live      # dashboard reads this back
        return live

    # ------------------------------------------------------------ decision
    def on_trades(self, trades):
        now = time.time()
        for t in trades:
            if not self.seen.is_new(t):
                continue

            self.refresh_settings()
            action, why, detail = engine.consider(self.s, t, self.pinned,
                                                  now=now)
            if action != "copy":
                if why != "not one of ours":
                    self.skips[why] = self.skips.get(why, 0) + 1
                    self.s["skipped"] += 1
                continue

            if cconfig.MODE == "live":
                ok, res = pm.place_order(detail["token"], "BUY",
                                         size_usd=detail["size"])
                if not ok:
                    self.skips[f"order failed: {res}"] = \
                        self.skips.get(f"order failed: {res}", 0) + 1
                    continue

            pos = engine.open_position(self.s, t, detail, now=now)
            self.copies += 1

            slug = t.get("slug")
            entry = self.pending.setdefault(slug, {"end": None, "tokens": {}})
            entry["tokens"][detail["token"]] = True
            if entry["end"] is None:
                parts = slug.rsplit("-", 1)
                if parts[-1].isdigit() and len(parts[-1]) >= 10:
                    entry["end"] = int(parts[-1]) + (t.get("horizon") or 300)

            self.pending_log.append({
                "event": "copy", "mode": cconfig.MODE,
                "trader": detail["trader"], "wallet": detail["wallet"],
                "market": slug, "coin": t.get("coin"),
                "outcome": t.get("outcome"),
                "price": detail["price"], "size": detail["size"],
                "shares": round(pos["shares"], 4),
                "their_usd": round((t.get("shares") or 0) * detail["price"], 2),
                "lag_seconds": round(now - (t.get("ts") or now), 2),
                "at": datetime.now(timezone.utc).isoformat(),
            })
            print(f"  COPY  {detail['trader'][:16]:<18} "
                  f"{t.get('coin','?'):>4} {t.get('outcome','?'):<5} "
                  f"{detail['price']*100:>3.0f}c  ${detail['size']:>6.2f}  "
                  f"{slug[:34]}", flush=True)

    # ---------------------------------------------------------- settlement
    def settle_due(self, now=None):
        now = now or time.time()
        closed = 0
        for slug in list(self.pending.keys()):
            entry = self.pending[slug]
            end = entry.get("end")
            if end is None:
                end = pm.fetch_market_end(slug)
                entry["end"] = end
                if end is None:
                    continue
            if now < end + settle.SETTLE_GRACE_SECONDS:
                continue

            win = settle.winning_asset(slug)
            if win is None:
                if now - end > settle.ABANDON_AFTER_SECONDS:
                    self.pending.pop(slug, None)
                    print(f"  ! {slug} never resolved", flush=True)
                continue

            for token in list(entry["tokens"]):
                pos = self.s["positions"].get(token)
                if pos is None:
                    continue
                won = str(token) == str(win)
                if cconfig.MODE == "live" and won:
                    pm.redeem(token)
                done = engine.settle_position(self.s, token, won, now=now)
                closed += 1
                self.pending_log.append({
                    "event": "settle", "mode": cconfig.MODE,
                    "trader": done["trader"], "market": slug,
                    "coin": done.get("coin"), "outcome": done.get("outcome"),
                    "price": done["price"], "cost": done["cost"],
                    "proceeds": round(done["proceeds"], 2),
                    "pnl": round(done["pnl"], 2),
                    "result": "WON" if won else "LOST",
                    "at": datetime.now(timezone.utc).isoformat(),
                })
                print(f"  {'WON ' if won else 'LOST'}  "
                      f"{done['trader'][:16]:<18} {done['pnl']:>+7.2f}  "
                      f"{slug[:34]}", flush=True)
            self.pending.pop(slug, None)
        self.settled += closed
        return closed

    # -------------------------------------------------------------- upkeep
    def persist(self):
        engine.forget_old_copies(self.s)
        self.s["last_run"] = datetime.now(timezone.utc).isoformat()
        cstate.save(self.s)
        cstate.log(self.pending_log)
        self.pending_log = []

    def report(self):
        s = self.s
        eq = engine.equity(s)
        drift = engine.books_drift(s)
        rolled = engine.roll_day(s)
        if rolled:
            print("  -- new day, daily stop reset --", flush=True)
        line = (f"  equity ${eq:.2f} | cash ${s['cash']:.2f} | "
                f"open {len(s['positions'])} (${engine.deployed(s):.2f}) | "
                f"realised {s['realized_pnl']:+.2f} | "
                f"{s['wins']}W/{s['losses']}L")
        if s["wins"] + s["losses"]:
            line += f" ({s['wins']/(s['wins']+s['losses'])*100:.0f}%)"
        loss = engine.day_loss_pct(s)
        if loss > 0:
            line += f" | today -{loss*100:.1f}%"
        print(line, flush=True)
        if abs(drift) > 0.05:
            print(f"  ! books out by ${drift:+.2f} — P&L cannot be trusted",
                  flush=True)
        if self.skips:
            top = sorted(self.skips.items(), key=lambda kv: -kv[1])[:4]
            print("  skipped: " + ", ".join(f"{n} {why}" for why, n in top),
                  flush=True)
            self.skips = {}


async def listen(bot, deadline):
    import websockets
    attempt = 0
    while time.time() < deadline:
        try:
            async with websockets.connect(
                    feed.URL, ping_interval=15, ping_timeout=45,
                    close_timeout=5, max_queue=2048) as ws:
                await ws.send(feed.subscribe_message())
                attempt = 0
                while time.time() < deadline:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(deadline - time.time(), 1))
                    bot.on_trades(feed.parse(raw))
        except asyncio.TimeoutError:
            return
        except Exception as e:
            if time.time() >= deadline:
                return
            bot.reconnects += 1
            wait = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            attempt += 1
            print(f"  reconnecting in {wait}s ({type(e).__name__})", flush=True)
            await asyncio.sleep(min(wait, max(deadline - time.time(), 0)))


async def housekeeping(bot, deadline):
    last_commit = time.time()
    while time.time() < deadline:
        await asyncio.sleep(min(SETTLE_EVERY, max(deadline - time.time(), 0)))
        if time.time() >= deadline:
            break
        await asyncio.to_thread(bot.settle_due)
        bot.report()
        await asyncio.to_thread(bot.persist)
        if time.time() - last_commit >= COMMIT_EVERY:
            if await asyncio.to_thread(
                    commit, datetime.now(timezone.utc).strftime("%H:%M")):
                print("  pushed", flush=True)
            last_commit = time.time()


async def main():
    bot = Bot()
    if not bot.pinned:
        print(f"No traders pinned in {cconfig.PINNED_FILE}.")
        print("Run:  python3 -m crypto.harvest 30 "
              "&& python3 -m crypto.weekly --pin")
        return 1

    if cconfig.MODE == "live" and not os.getenv("POLY_PRIVATE_KEY"):
        print("live mode asked for, no wallet key — staying on paper")
        cconfig.MODE = "paper"

    deadline = time.time() + WINDOW_MINUTES * 60
    print(f"=== crypto copy bot | {cconfig.MODE} | "
          f"${cconfig.TRADING_ALLOCATION:.0f} allocation ===")
    print(f"  following {len(bot.pinned)} traders")
    print(f"  {cconfig.STAKE_PCT*100:.1f}% per trade, "
          f"{cconfig.MAX_DEPLOYED_PCT*100:.0f}% deployed max, "
          f"{cconfig.DAILY_STOP_PCT*100:.0f}% daily stop")
    print(f"  {cconfig.MIN_HORIZON_SECONDS//60}m-"
          f"{cconfig.MAX_HORIZON_SECONDS//60}m markets, "
          f"{cconfig.MIN_BUY_PRICE*100:.0f}c-{cconfig.MAX_BUY_PRICE*100:.0f}c")
    print(f"  listening for {WINDOW_MINUTES} minutes\n")

    await asyncio.gather(listen(bot, deadline), housekeeping(bot, deadline))

    bot.settle_due()
    bot.persist()
    bot.report()
    commit("final")
    print(f"\ncopied {bot.copies} | settled {bot.settled} | "
          f"reconnects {bot.reconnects}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
