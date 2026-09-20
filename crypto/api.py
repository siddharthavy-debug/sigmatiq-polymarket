"""
Polymarket API access for the crypto engine.

Self-contained on purpose — the sports bot is gone and nothing here depends on
it. Read endpoints are public; only place_order and redeem touch a wallet, and
they refuse to do anything without a key.

Endpoint notes worth keeping:
  * Gamma hides settled markets from its default query. Anything historical
    needs closed=true, and forgetting that made 2,880 of 3,242 markets look
    unresolved.
  * The winning token is found by matching outcomePrices against clobTokenIds
    by index. Assuming [Yes, No] order instead turns every win into a loss.
  * /positions returns CURRENT holdings only — redeemed winners are deleted,
    so nobody's win rate can be reconstructed from it. We measure our own.
"""
import json
import os
import time

import requests

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
POLY_FUNDER = os.getenv("POLY_FUNDER", "")

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
TIMEOUT = 25


def _get(url, params=None, tries=3):
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS,
                             timeout=TIMEOUT)
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
    if isinstance(data, dict):
        for key in ("data", "results", "items", "markets"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return []


def _parse_ts(value):
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


def _maybe_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


_end_cache = {}


def fetch_market_end(slug):
    """When this market settles, epoch seconds. None if unknown."""
    if not slug:
        return None
    if slug in _end_cache:
        return _end_cache[slug]
    end = None
    for params in ({"slug": slug, "limit": 1},
                   {"slug": slug, "limit": 1, "closed": "true"}):
        rows = _rows(_get(f"{GAMMA_API}/markets", params, tries=1))
        if rows:
            m = rows[0]
            end = (_parse_ts(m.get("endDate"))
                   or _parse_ts(m.get("end_date_iso"))
                   or _parse_ts(m.get("closedTime")))
            break
    _end_cache[slug] = end
    return end


def activity(wallet, limit=500, offset=0):
    return _rows(_get(f"{DATA_API}/activity",
                      {"user": wallet, "limit": limit,
                       "offset": offset, "type": "TRADE"}))


# ----------------------------------------------------------- live execution
_clob = None


def _client():
    global _clob
    if _clob is not None:
        return _clob
    if not POLY_PRIVATE_KEY:
        raise RuntimeError("POLY_PRIVATE_KEY not set — cannot trade live")
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    _clob = ClobClient(host=CLOB_API, key=POLY_PRIVATE_KEY, chain_id=POLYGON,
                       signature_type=1, funder=POLY_FUNDER or None)
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
            side=side.upper())
        signed = client.create_market_order(args)
        resp = client.post_order(signed, OrderType.FOK)
        return bool(resp.get("success", True)), resp
    except Exception as e:
        return False, str(e)


def redeem(token_id):
    """Collect winnings from a resolved position. Without this the USDC stays
    locked and the wallet runs dry despite winning."""
    try:
        client = _client()
        if hasattr(client, "redeem_positions"):
            return True, client.redeem_positions(token_id)
        return place_order(token_id, "SELL", shares=None)
    except Exception as e:
        return False, str(e)
