"""
The recorder. Listens, settles, remembers — trades nothing.

    python3 -m crypto.record                  # 5.5h window (GitHub Actions)
    WINDOW_MINUTES=5 python3 -m crypto.record # short local test

It exists because we can't rank crypto traders from Polymarket's public API:
winning positions are deleted once redeemed. So instead of reconstructing
history we build our own, by watching every trade as it happens and checking
the result minutes later when the market resolves.

Nothing here places an order or touches the paper-trading bot.
"""
import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import feed, settle, store

WINDOW_MINUTES = int(os.getenv("WINDOW_MINUTES", "330"))
SETTLE_EVERY = int(os.getenv("SETTLE_EVERY_SECONDS", "120"))
COMMIT_EVERY = int(os.getenv("COMMIT_EVERY_SECONDS", "600"))
IN_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"

RECONNECT_BACKOFF = [1, 2, 5, 10, 20, 30]


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def commit(label):
    """Same push dance as the trading bot: detached HEAD, and someone else
    may have pushed while we were listening."""
    if not IN_ACTIONS:
        return False
    branch = os.getenv("GITHUB_REF_NAME") or "main"
    _git("config", "user.name", "recorder")
    _git("config", "user.email", "recorder@users.noreply.github.com")
    if os.path.isdir(".git/rebase-merge") or os.path.isdir(".git/rebase-apply"):
        _git("rebase", "--abort")
    _git("add", "data/crypto/")
    if _git("diff", "--staged", "--quiet").returncode == 0:
        return False
    _git("commit", "-m", f"crypto history {label}")
    if _git("push", "origin", f"HEAD:{branch}").returncode == 0:
        return True
    _git("fetch", "origin", branch)
    if _git("rebase", "-X", "theirs", f"origin/{branch}").returncode != 0:
        _git("rebase", "--abort")
        return False
    return _git("push", "origin", f"HEAD:{branch}").returncode == 0


class Recorder:
    def __init__(self):
        self.pending = store.load_pending()
        self.wallets = store.load_wallets()
        self.seen = feed.Seen()
        self.captured = 0
        self.settled_markets = 0
        self.settled_positions = 0
        self.reconnects = 0

    def capture(self, trades):
        now = time.time()
        for t in trades:
            if not self.seen.is_new(t):
                continue
            slug = t["slug"]
            entry = self.pending.get(slug)
            if entry is None:
                # A 5m market ends 5 minutes after it opens; deriving the end
                # from the slug's timestamp avoids a Gamma call per market,
                # and settle() falls back to Gamma when it can't be derived.
                end = None
                parts = slug.rsplit("-", 1)
                if parts[-1].isdigit() and len(parts[-1]) >= 10:
                    start = int(parts[-1])
                    end = start + (t.get("horizon") or 300)
                entry = self.pending[slug] = {
                    "end": end, "first_seen": now, "trades": [],
                }
            entry["trades"].append(t)
            self.captured += 1

    def settle(self, verbose=True):
        markets, positions, still = settle.settle_due(
            self.pending, self.wallets, verbose=verbose)
        self.settled_markets += markets
        self.settled_positions += positions
        return markets, positions, still

    def persist(self):
        store.save_pending(self.pending)
        self.wallets = store.save_wallets(self.wallets)


async def listen(rec, deadline):
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
                    remaining = deadline - time.time()
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    rec.capture(feed.parse(raw))
        except asyncio.TimeoutError:
            return
        except Exception as e:
            if time.time() >= deadline:
                return
            rec.reconnects += 1
            wait = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            attempt += 1
            print(f"  reconnecting in {wait}s ({type(e).__name__})", flush=True)
            await asyncio.sleep(min(wait, max(deadline - time.time(), 0)))


async def housekeeping(rec, deadline):
    """Settle resolved markets and commit, while the listener keeps listening."""
    last_commit = time.time()
    while time.time() < deadline:
        await asyncio.sleep(min(SETTLE_EVERY, max(deadline - time.time(), 0)))
        if time.time() >= deadline:
            break

        markets, positions, still = await asyncio.to_thread(rec.settle)
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  {stamp}  captured {rec.captured:,}  settled {markets} markets"
              f" / {positions} positions  pending {still}"
              f"  wallets {len(rec.wallets):,}", flush=True)

        await asyncio.to_thread(rec.persist)
        if time.time() - last_commit >= COMMIT_EVERY:
            if await asyncio.to_thread(
                    commit, datetime.now(timezone.utc).strftime("%H:%M")):
                print("  pushed", flush=True)
            last_commit = time.time()


async def main():
    rec = Recorder()
    deadline = time.time() + WINDOW_MINUTES * 60
    print(f"recorder: listening for {WINDOW_MINUTES} minutes", flush=True)
    print(f"  carrying {len(rec.wallets):,} wallets, "
          f"{len(rec.pending)} markets mid-flight", flush=True)

    await asyncio.gather(listen(rec, deadline), housekeeping(rec, deadline))

    rec.settle()
    rec.persist()
    commit("final")

    print(f"\ncaptured {rec.captured:,} trades")
    print(f"settled  {rec.settled_markets:,} markets / "
          f"{rec.settled_positions:,} positions")
    print(f"wallets  {len(rec.wallets):,}   reconnects {rec.reconnects}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
