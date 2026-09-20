"""
The Polymarket real-time feed.

    wss://ws-live-data.polymarket.com   topic "activity", type "trades"

Verified against the live feed on 17 Sep 2026: 452 of 452 messages carried a
proxyWallet, arriving 0.4-1.4s after the trade. That is the whole reason the
crypto design is possible — the CLOB market socket publishes trades with no
identity at all.

Two things the live test taught us that aren't in the docs:

  * Every match is published TWICE, once per side. The two messages share a
    transactionHash but carry different wallets and opposite outcome tokens.
    Both are real trades by real people, so both are kept — but a genuine
    duplicate (same wallet, same token, same hash) is dropped.

  * The connection dies roughly every two minutes with a keepalive timeout,
    so reconnecting is normal operation, not an error.
"""
import json
import re
import time

URL = "wss://ws-live-data.polymarket.com"

# btc-updown-5m-<ts>, eth-updown-15m-<ts>, sol-up-or-down-september-17-...
CRYPTO_SLUG = re.compile(
    r"^([a-z0-9]{2,6})-(?:updown|up-or-down)[-a-z0-9]*?(?:-(\d{5,}))?$")

# guard against a stray non-crypto market matching the pattern
COINS = {"btc", "eth", "sol", "xrp", "doge", "bnb", "hype", "ltc", "ada",
         "link", "avax", "trx", "dot", "matic", "shib", "pepe", "sui"}


def classify(slug):
    """-> (coin, horizon_seconds) or None if this isn't a crypto up/down market."""
    if not slug:
        return None
    m = CRYPTO_SLUG.match(slug)
    if not m:
        return None
    coin = m.group(1)
    if coin not in COINS:
        return None
    horizon = None
    h = re.search(r"-(\d+)(m|h)-", slug)
    if h:
        horizon = int(h.group(1)) * (60 if h.group(2) == "m" else 3600)
    return coin, horizon


def parse(raw):
    """One websocket frame -> list of trade dicts we care about."""
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if msg.get("topic") != "activity":
        return []

    payload = msg.get("payload")
    items = payload if isinstance(payload, list) else [payload]

    out = []
    for t in items:
        if not isinstance(t, dict):
            continue
        wallet = t.get("proxyWallet")
        slug = t.get("slug") or t.get("eventSlug")
        price = t.get("price")
        shares = t.get("size")
        if not (wallet and slug and price and shares):
            continue

        kind = classify(slug)
        if kind is None:
            continue
        coin, horizon = kind

        ts = t.get("timestamp")
        try:
            ts = float(ts)
            if ts > 1e11:          # milliseconds
                ts /= 1000.0
        except (TypeError, ValueError):
            ts = time.time()

        out.append({
            "slug": slug,
            "coin": coin,
            "horizon": horizon,
            "wallet": wallet,
            "name": t.get("name") or t.get("pseudonym") or wallet[:10],
            "asset": t.get("asset"),
            "outcome": t.get("outcome"),
            "outcome_index": t.get("outcomeIndex"),
            "side": t.get("side"),
            "price": float(price),
            "shares": float(shares),
            "ts": ts,
            "tx": t.get("transactionHash"),
        })
    return out


def subscribe_message():
    return json.dumps({
        "action": "subscribe",
        "subscriptions": [{"topic": "activity", "type": "trades"}],
    })


class Seen:
    """
    Drop genuine duplicates without growing forever.

    Keyed on (tx, wallet, asset, side) — the two sides of one match differ by
    wallet and asset, so they survive; a replayed frame after a reconnect does
    not.
    """

    def __init__(self, ttl=900):
        self.ttl = ttl
        self._seen = {}
        self._swept = time.time()

    def is_new(self, trade):
        key = (trade.get("tx"), trade["wallet"], trade.get("asset"),
               trade.get("side"))
        now = time.time()
        if now - self._swept > self.ttl:
            cutoff = now - self.ttl
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
            self._swept = now
        if key in self._seen:
            return False
        self._seen[key] = now
        return True
