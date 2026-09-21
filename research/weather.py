#!/usr/bin/env python3
"""
weather.py -- do Polymarket's weather markets misprice the forecast?

The bet: a public numerical forecast is better than the crowd, and the crowd
is slow to update. If that is true there is a repeatable edge; if it is not,
we drop this and stop wondering.

Two stages, because we do not yet know the shape of these markets.

  Stage 1 (discover): find settled weather markets, parse out the city, the
  threshold and the date, and PRINT WHAT IT COULD NOT PARSE. Never assume the
  slug format -- the crypto bot cost us a day by assuming one.

  Stage 2 (score): for each parsed market, ask Open-Meteo what its forecast
  was at the time the market was priced (their historical-forecast archive
  stores the forecast as issued, not the hindsight truth), turn that into a
  probability, and compare with what the market charged.

Read-only.

    python3 -m research.weather discover
    python3 -m research.weather score
"""
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

from crypto import api

RAW = "research/weather_markets.json"
OUT = "research/weather_result.json"

ARCHIVE = "https://historical-forecast-api.open-meteo.com/v1/forecast"

KEYWORDS = ("temperature", "highest temperature", "weather", "rain",
            "snow", "hurricane", "degrees", "temp in", "warmest")

# Filled from what discovery actually finds; seeded with the obvious ones.
CITIES = {
    "nyc": (40.7128, -74.0060), "new york": (40.7128, -74.0060),
    "london": (51.5072, -0.1276), "paris": (48.8566, 2.3522),
    "moscow": (55.7558, 37.6173), "seoul": (37.5665, 126.9780),
    "tokyo": (35.6762, 139.6503), "beijing": (39.9042, 116.4074),
    "los angeles": (34.0522, -118.2437), "la": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298), "miami": (25.7617, -80.1918),
    "denver": (39.7392, -104.9903), "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740), "seattle": (47.6062, -122.3321),
    "boston": (42.3601, -71.0589), "atlanta": (33.7490, -84.3880),
    "dallas": (32.7767, -96.7970), "philadelphia": (39.9526, -75.1652),
    "washington": (38.9072, -77.0369), "dc": (38.9072, -77.0369),
    "berlin": (52.5200, 13.4050), "madrid": (40.4168, -3.7038),
    "rome": (41.9028, 12.4964), "dubai": (25.2048, 55.2708),
    "delhi": (28.6139, 77.2090), "mumbai": (19.0760, 72.8777),
    "sydney": (-33.8688, 151.2093), "toronto": (43.6532, -79.3832),
    "mexico city": (19.4326, -99.1332), "sao paulo": (-23.5505, -46.6333),
    "buenos aires": (-34.6037, -58.3816), "cairo": (30.0444, 31.2357),
    "istanbul": (41.0082, 28.9784), "singapore": (1.3521, 103.8198),
    "hong kong": (22.3193, 114.1694), "bangkok": (13.7563, 100.5018),
    "jakarta": (-6.2088, 106.8456), "lagos": (6.5244, 3.3792),
    "johannesburg": (-26.2041, 28.0473), "amsterdam": (52.3676, 4.9041),
    "stockholm": (59.3293, 18.0686), "vancouver": (49.2827, -123.1207),
}

# "highest-temperature-in-nyc-on-march-4" / "...-above-60f" and friends.
THRESH = re.compile(r"(above|below|over|under|at[- _]least|exceeds?|"
                    r"higher[- _]than|greater[- _]than|less[- _]than|"
                    r"under|reach(?:es)?)"
                    r"[-_ ]+(\d{1,3})", re.I)
DEGREE = re.compile(r"(\d{1,3})\s*(?:°|deg|degrees)?\s*([fc])\b", re.I)


def find_city(text):
    # slugs are hyphenated, questions are not -- flatten both to spaces or
    # "los-angeles" never matches the key "los angeles".
    t = re.sub(r"[-_]+", " ", text.lower())
    hit, best = None, -1
    for name, coords in CITIES.items():
        # longest name wins so "new york" beats a stray "la"
        if re.search(rf"\b{re.escape(name)}\b", t) and len(name) > best:
            hit, best = (name, coords), len(name)
    return hit


def discover(limit=6000):
    print(f"scanning up to {limit} settled markets for weather\n")
    found, offset, page, scanned = [], 0, 500, 0
    while scanned < limit:
        rows = api._rows(api._get(
            f"{api.GAMMA_API}/markets",
            {"closed": "true", "limit": page, "offset": offset,
             "order": "endDate", "ascending": "false"}))
        if not rows:
            break
        scanned += len(rows)
        offset += page
        for m in rows:
            blob = f"{m.get('slug','')} {m.get('question','')}".lower()
            if any(k in blob for k in KEYWORDS):
                found.append(m)
        print(f"  scanned {scanned}  weather so far {len(found)}")
        time.sleep(0.25)

    print(f"\n{len(found)} weather-ish markets\n")

    parsed, unparsed = [], []
    by_city = defaultdict(int)
    for m in found:
        blob = f"{m.get('slug','')} {m.get('question','')}"
        city = find_city(blob)
        th = THRESH.search(m.get("slug", "")) or DEGREE.search(m.get("question", ""))
        if city and th:
            by_city[city[0]] += 1
            parsed.append({
                "slug": m.get("slug"), "question": m.get("question"),
                "city": city[0], "lat": city[1][0], "lon": city[1][1],
                "threshold": th.group(2), "direction": th.group(1).lower(),
                "end": m.get("endDate"),
                "volume": float(m.get("volumeNum") or 0),
                "outcomePrices": m.get("outcomePrices"),
                "clobTokenIds": m.get("clobTokenIds"),
                "outcomes": m.get("outcomes"),
            })
        else:
            unparsed.append({"slug": m.get("slug"),
                             "question": m.get("question"),
                             "missing": ("city" if not city else "") +
                                        ("threshold" if not th else "")})

    print(f"parsed   {len(parsed)}")
    print(f"unparsed {len(unparsed)}   <-- read these before trusting anything\n")
    print("cities by market count:")
    for c, n in sorted(by_city.items(), key=lambda kv: -kv[1]):
        print(f"  {c:<16} {n}")
    print("\nfirst 25 unparsed:")
    for u in unparsed[:25]:
        print(f"  [{u['missing'] or 'other'}] {u['slug']}")

    os.makedirs("research", exist_ok=True)
    with open(RAW, "w") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(),
                   "parsed": parsed, "unparsed": unparsed}, f, indent=2)
    print(f"\nwrote {RAW}  ({len(parsed)} usable)")


def forecast_high(lat, lon, date, lead_days=1):
    """The daily high Open-Meteo was PREDICTING `lead_days` before `date`."""
    r = requests.get(ARCHIVE, params={
        "latitude": lat, "longitude": lon,
        "start_date": date, "end_date": date,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        # the archive stores each forecast run; this asks for the run issued
        # `lead_days` earlier rather than the final analysis
        "past_days": 0,
    }, timeout=30)
    r.raise_for_status()
    d = r.json().get("daily", {})
    vals = d.get("temperature_2m_max") or []
    return vals[0] if vals else None


def score():
    if not os.path.exists(RAW):
        print(f"{RAW} missing -- run discover first")
        return
    data = json.load(open(RAW))
    rows = data["parsed"]
    print(f"scoring {len(rows)} markets\n")

    out, skipped = [], defaultdict(int)
    for i, m in enumerate(rows):
        if i % 25 == 0 and i:
            print(f"  {i}/{len(rows)}  usable={len(out)}")
        end = m.get("end")
        if not end:
            skipped["no_date"] += 1
            continue
        date = end[:10]
        try:
            fc = forecast_high(m["lat"], m["lon"], date)
        except Exception as e:
            skipped[f"api:{type(e).__name__}"] += 1
            time.sleep(1)
            continue
        if fc is None:
            skipped["no_forecast"] += 1
            continue

        thr = float(m["threshold"])
        d = re.sub(r"[-_ ]+", "", m["direction"].lower())
        above = d in ("above", "over", "atleast", "exceed", "exceeds",
                      "higherthan", "greaterthan", "reach", "reaches")
        margin = (fc - thr) if above else (thr - fc)

        prices = api._maybe_json(m.get("outcomePrices"))
        try:
            settle = [float(x) for x in (prices or [])]
        except (TypeError, ValueError):
            skipped["malformed"] += 1
            continue
        if not settle or abs(sum(settle) - 1.0) > 0.01:
            skipped["unresolved"] += 1
            continue

        out.append({
            "slug": m["slug"], "city": m["city"], "date": date,
            "threshold": thr, "direction": m["direction"],
            "forecast_high": fc, "margin_f": round(margin, 2),
            "yes_won": 1 if settle[0] > 0.5 else 0,
            "volume": m["volume"],
        })
        time.sleep(0.15)

    print(f"\n{len(out)} scored  skipped={dict(skipped)}\n")
    report(out)
    with open(OUT, "w") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(),
                   "n": len(out), "rows": out}, f, indent=2)
    print(f"wrote {OUT}")


def report(rows):
    if not rows:
        print("nothing scored")
        return
    print("How often the forecast was right, by how clear the call was:")
    print("margin            n    forecast correct")
    print("-" * 46)
    bands = [(8, 999), (4, 8), (2, 4), (1, 2), (0, 1), (-999, 0)]
    for lo, hi in bands:
        sel = [r for r in rows if lo <= r["margin_f"] < hi]
        if not sel:
            continue
        right = sum(1 for r in sel
                    if (r["margin_f"] > 0) == (r["yes_won"] == 1))
        label = f"{lo:+g} to {hi:+g}F" if hi < 999 else f"{lo:+g}F and up"
        print(f"{label:<16} {len(sel):>4}   {right/len(sel)*100:>5.1f}%")
    print("-" * 46)
    by_city = defaultdict(lambda: [0, 0])
    for r in rows:
        c = by_city[r["city"]]
        c[0] += 1
        c[1] += 1 if (r["margin_f"] > 0) == (r["yes_won"] == 1) else 0
    print("\nby city (forecast accuracy):")
    for c, (n, ok) in sorted(by_city.items(), key=lambda kv: -kv[1][0]):
        if n >= 5:
            print(f"  {c:<16} {n:>4}  {ok/n*100:>5.1f}%")
    print("\nAccuracy alone is not an edge -- stage 3 compares this against")
    print("the price the market charged at the same moment.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "discover"
    if cmd == "discover":
        discover(int(sys.argv[2]) if len(sys.argv) > 2 else 6000)
    elif cmd == "score":
        score()
    else:
        print(__doc__)
