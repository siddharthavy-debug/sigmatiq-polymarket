"""
Polymarket API access.

Read endpoints are public — no key, no account. Only live order placement
touches the wallet, and that's isolated in place_order().

Endpoint notes learned the hard way:
  * Leaderboard is /v1/leaderboard?period=&limit=  (not /leaderboard?window=)
  * /positions returns CURRENT holdings only. Redeemed winners vanish from it,
    so it cannot be used to reconstruct anyone's win rate. We don't try.
  * /activity is the live trade feed we copy from.
"""
import time
import requests

from . import config

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
TIMEOUT = 25


def _get(url, params=None, tries=3):
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (400, 404):
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == tries - 1:
                print(f"    api error: {e}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def _rows(data):
    if data is None:
        return []
    if isinstance(data, list):
        return data
    for k in ("data", "leaderboard", "items", "results"):
        if isinstance(data.get(k), list):
            return data[k]
    return []


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------- leaderboard
def fetch_top_traders(limit=None, period=None, verbose=True):
    """
    Top wallets by P&L. Both pnl and vol come straight from Polymarket and
    are trustworthy. Win rate is deliberately absent — it isn't available
    from the public API, and our analyzer measures it from real results.
    """
    limit = limit or config.TOP_N_TRADERS
    period = period or config.LEADERBOARD_PERIOD

    rows = _rows(_get(f"{config.DATA_API}/v1/leaderboard",
                      {"period": period, "limit": max(limit * 4, 100)}))
    if not rows:
        return []

    out = []
    rejected_volume = rejected_pnl = 0
    for row in rows:
        wallet = row.get("proxyWallet") or row.get("wallet") or row.get("address")
        if not wallet:
            continue
        pnl = _num(row.get("pnl"))
        vol = _num(row.get("vol") or row.get("volume"))

        if vol < config.MIN_TRADER_VOLUME:      # inactive this window
            rejected_volume += 1
            continue
        if pnl < config.MIN_TRADER_PNL:         # not actually up
            rejected_pnl += 1
            continue

        out.append({
            "wallet": wallet,
            "name": row.get("userName") or row.get("name") or wallet[:12],
            "pnl": pnl,
            "volume": vol,
        })
        if len(out) >= limit:
            break

    if verbose:
        print(f"[leaderboard] {len(rows)} wallets returned, {len(out)} qualified "
              f"({rejected_volume} under ${config.MIN_TRADER_VOLUME:,.0f} volume, "
              f"{rejected_pnl} under ${config.MIN_TRADER_PNL:,.0f} profit)")
    return out


# ---------------------------------------------------------- trader activity
def fetch_trader_activity(wallet, limit=50):
    """Recent trades by one wallet. This is what we copy."""
    rows = _rows(_get(f"{config.DATA_API}/activity",
                      {"user": wallet, "limit": limit, "type": "TRADE"}))
    return rows or []


def fetch_price(token_id, side="BUY"):
    data = _get(f"{config.CLOB_API}/price",
                {"token_id": token_id, "side": side.lower()})
    return _num(data.get("price")) if data else None


# ----------------------------------------------------------- live execution
_clob = None


def _client():
    global _clob
    if _clob is not None:
        return _clob
    if not config.POLY_PRIVATE_KEY:
        raise RuntimeError("POLY_PRIVATE_KEY not set — cannot trade live")
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON

    _clob = ClobClient(
        host=config.CLOB_API,
        key=config.POLY_PRIVATE_KEY,
        chain_id=POLYGON,
        signature_type=1,
        funder=config.POLY_FUNDER or None,
    )
    _clob.set_api_creds(_clob.create_or_derive_api_creds())
    return _clob


def place_order(token_id, side, size_usd=None, shares=None):
    """Real order. BUY takes size_usd, SELL takes shares. -> (ok, detail)"""
    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        client = _client()
        args = MarketOrderArgs(
            token_id=token_id,
            amount=float(size_usd if side.upper() == "BUY" else shares),
            side=side.upper(),
        )
        signed = client.create_market_order(args)
        resp = client.post_order(signed, OrderType.FOK)
        return bool(resp.get("success", True)), resp
    except Exception as e:
        return False, str(e)


def redeem(token_id):
    """
    Live mode: collect winnings from a resolved position. Without this the
    USDC stays locked in the settled position and the bot slowly runs out of
    cash despite winning.
    """
    try:
        client = _client()
        if hasattr(client, "redeem_positions"):
            return True, client.redeem_positions(token_id)
        # Older client versions expose it differently; selling a resolved
        # winner at 1.0 achieves the same settlement.
        return place_order(token_id, "SELL", shares=None)
    except Exception as e:
        return False, str(e)


# ------------------------------------------------------------ market horizon
GAMMA_API = "https://gamma-api.polymarket.com"
_end_cache = {}


def _parse_ts(value):
    """ISO string or epoch -> epoch seconds, or None."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fetch_market_end(slug):
    """
    When this market settles, as epoch seconds. None if unknown.

    Cached, because the bot asks once per new market and the answer doesn't
    change. Unknown is returned as None and the caller decides — we don't
    want a Gamma hiccup to silently block all trading.
    """
    if not slug:
        return None
    if slug in _end_cache:
        return _end_cache[slug]

    end = None
    # One attempt per query. This runs once per new market and an unknown
    # answer is handled gracefully, so retrying just makes the bot slow when
    # Gamma is having a bad day.
    #
    # The live bot mostly asks about markets that are still trading, so the
    # plain query comes first here; closed=true is the fallback, since Gamma
    # hides settled markets from the default query.
    m = None
    for params in ({"slug": slug, "limit": 1},
                   {"slug": slug, "limit": 1, "closed": "true"}):
        rows = _rows(_get(f"{GAMMA_API}/markets", params, tries=1))
        if rows:
            m = rows[0]
            break
    if m:
        end = (_parse_ts(m.get("endDate"))
               or _parse_ts(m.get("end_date_iso"))
               or _parse_ts(m.get("endDateIso")))
    _end_cache[slug] = end
    return end
