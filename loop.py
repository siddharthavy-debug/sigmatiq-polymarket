"""
Continuous mode — polls every 30 seconds instead of waking on a timer.

The scheduled version does one pass and exits, so a trade made at 10:01 isn't
copied until the next wake-up. That gap is the lag, and making the scan faster
doesn't fix it: the trade was already old when the bot opened its eyes.

This keeps the process alive, which is how a bot running in a terminal behaves.
On GitHub Actions a single job can run about 6 hours, so it loops for a window
just under that and the schedule starts a fresh one.

State commits back to git periodically, not every pass — at one commit per 30
seconds the repo history would be unusable.

    python3 loop.py                    # 30s polling, 5.5h window
    POLL_SECONDS=15 python3 loop.py    # faster
    WINDOW_MINUTES=10 python3 loop.py  # short test
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
WINDOW_MINUTES = int(os.getenv("WINDOW_MINUTES", "330"))        # 5.5 hours
COMMIT_EVERY_SECONDS = int(os.getenv("COMMIT_EVERY_SECONDS", "300"))
IN_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"

MAX_CONSECUTIVE_ERRORS = 10


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _branch():
    return os.getenv("GITHUB_REF_NAME") or "main"


def commit_state(label):
    """
    Push data/ back to the repo.

    Two things bite here and both have. The checkout can leave the job on a
    detached HEAD, so a bare `git push` fails with "You are not currently on a
    branch" — pushing HEAD:<branch> explicitly avoids that. And when someone
    pushes to the repo while the job is running, the push is rejected; the fix
    is to rebase onto the remote, preferring OUR data files, since the bot's
    in-memory state is newer than anything on the remote.
    """
    if not IN_ACTIONS:
        return False

    branch = _branch()
    _git("config", "user.name", "copybot")
    _git("config", "user.email", "copybot@users.noreply.github.com")

    # a stuck rebase from a previous cycle would break everything after it
    if os.path.isdir(".git/rebase-merge") or os.path.isdir(".git/rebase-apply"):
        _git("rebase", "--abort")

    _git("add", "data/")
    if _git("diff", "--staged", "--quiet").returncode == 0:
        return False                          # nothing changed
    _git("commit", "-m", f"state {label}")

    if _git("push", "origin", f"HEAD:{branch}").returncode == 0:
        return True

    # rejected — someone else pushed. Rebase onto them, keeping our data.
    _git("fetch", "origin", branch)
    # in a rebase the replayed commit is "theirs", so -X theirs keeps ours
    if _git("rebase", "-X", "theirs", f"origin/{branch}").returncode != 0:
        _git("rebase", "--abort")
        print("  ! could not reconcile with the remote; will retry next cycle",
              flush=True)
        return False

    push = _git("push", "origin", f"HEAD:{branch}")
    if push.returncode != 0:
        print(f"  ! push failed: {push.stderr.strip()[:200]}", flush=True)
        return False
    return True


def main():
    from bot.copybot import run

    started = time.time()
    deadline = started + WINDOW_MINUTES * 60
    last_commit = started
    passes = 0
    errors = 0

    print(f"continuous mode: every {POLL_SECONDS}s for {WINDOW_MINUTES} minutes",
          flush=True)

    while time.time() < deadline:
        passes += 1
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"\n--- pass {passes} · {stamp} UTC ---", flush=True)

        try:
            run()
            errors = 0
        except KeyboardInterrupt:
            print("\nstopped", flush=True)
            break
        except Exception as e:
            errors += 1
            print(f"  ! pass failed: {e}", flush=True)
            if errors >= MAX_CONSECUTIVE_ERRORS:
                print(f"{errors} failures in a row — stopping", flush=True)
                break
            # Back off, but never sleep past the end of the window.
            backoff = min(errors * 15, 120, max(deadline - time.time(), 0))
            if backoff <= 0:
                break
            time.sleep(backoff)
            continue

        if time.time() - last_commit >= COMMIT_EVERY_SECONDS:
            if commit_state(datetime.now(timezone.utc).strftime("%H:%M")):
                print("  state pushed", flush=True)
            last_commit = time.time()

        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(POLL_SECONDS, remaining))

    commit_state("final")
    mins = (time.time() - started) / 60
    print(f"\nfinished: {passes} passes over {mins:.0f} minutes", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
