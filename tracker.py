#!/usr/bin/env python3
"""
Polymarket Whale Tracker
========================
Checks a list of Polymarket wallets for new trades since the last run and
posts them to Discord as clean, easy-to-read alerts.

What it does
  * Combines split fills: one order filled in 5 pieces = ONE alert.
  * Units: sizes every play against that wallet's typical (median) bet,
    so a $500 play from a $100 bettor shows as 5.0u.
  * Grades the wallet's past plays by size (<1u, 1-3u, 3-5u, 5u+) and
    flags new plays in its proven buckets:
        🟢 GREEN LIGHT - this size has made money for this wallet
        🔴 FADE        - this size has lost money for this wallet
  * Posts a weekly report card for each wallet (record + ROI by size).

  * Data source : Polymarket's free public Data API v2 (no API key needed)
  * Dependencies: none - Python 3.8+ standard library only
  * State       : state.json remembers what was posted + each wallet's stats

The SAME script is used for both setups:
  * Local          -> python tracker.py   (reads wallets.txt + SETTINGS below)
  * GitHub Actions -> reads the WALLETS and DISCORD_WEBHOOK_URL secrets

Any setting below can be overridden with an environment variable of the
same name, which is how the GitHub Actions version gets its secrets.
"""

import hashlib
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# SETTINGS  (edit these for local use; GitHub Actions overrides them)
# ---------------------------------------------------------------------------

# Local use: paste your Discord webhook URL between the quotes.
# GitHub Actions: LEAVE THIS BLANK and use the DISCORD_WEBHOOK_URL secret.
DISCORD_WEBHOOK_URL = ""

# Plays smaller than this many USDC are ignored, unless the wallet has its
# own minimum in the wallet list. Split fills are added up first, so a
# $600 order filled as 3 x $200 counts as $600.
DEFAULT_MIN_TRADE_USD = 100

# When a wallet is seen for the first time, how far back to look for trades
# to post. 0 = only trades made after the wallet was added.
FIRST_RUN_LOOKBACK_HOURS = 0

# --- Units & grading ---
HISTORY_DAYS = 180           # how far back to look when grading past plays
MIN_SAMPLE = 15              # graded plays needed before a size can earn
                             # a 🟢 or 🔴 (fewer = could just be luck)
GREEN_LIGHT_MIN_ROI = 10     # bucket ROI (%) at or above this -> 🟢 GREEN LIGHT
FADE_MAX_ROI = -10           # bucket ROI (%) at or below this -> 🔴 FADE
PROFILE_REFRESH_DAYS = 7     # re-grade each wallet (and post its report) weekly
WEEKLY_REPORT = 1            # 1 = post the weekly report card, 0 = don't

# Which plays get posted to Discord (applies to every wallet):
#   "green" - only 🟢 GREEN LIGHT plays (sizes where that wallet is proven
#             to win) and 🟡 BIG PLAYs. Average plays, fades, sells skipped.
#   "signals" - 🟢 GREEN LIGHT and 🔴 FADE plays
#   "all"   - every play above the wallet's minimum
ALERTS = "green"

# 🟡 BIG PLAY: also post buys this many units or bigger when the wallet is
# winning at that size but doesn't have enough plays yet for a green light.
# 0 = turn off.
BIG_PLAY_UNITS = 3

# --- 🔵 Whale Model official picks ---
# The best plays across ALL your wallets, posted as official picks.
OFFICIAL_PICKS = 1           # 1 = on, 0 = off
OFFICIAL_WEBHOOK_URL = ""    # separate channel for official picks (local use);
                             # on GitHub use the OFFICIAL_WEBHOOK_URL secret.
                             # Blank = post in the main channel.
OFFICIAL_MIN_SAFE_ROI = 5    # bucket ROI minus its luck range must be >= this %
OFFICIAL_MIN_UNITS = 2       # only the wallet's bigger bets (this many units+)
CONSENSUS_HOURS = 24         # 2+ wallets on the same side within this = pick
COLD_AFTER = 10              # wallet's graded alerts needed before "cold" check

# --- Alert scorecard ---
# Every 🟢 / 🟡 alert is remembered and graded once its market settles, as if
# you bet 1u on it at the alert price. A scorecard posts every SCORECARD_DAYS.
SCORECARD_DAYS = 7           # 0 = never post the scorecard
GRADE_EVERY_HOURS = 3        # how often to check whether alerted games settled
ALERT_EXPIRE_DAYS = 30       # alerts still unsettled after this are dropped

# Adds a "Bet on Novig" link to every buy alert. Paste a different address
# here if the one you use is different; leave blank ("") to hide the link.
NOVIG_URL = ""

# Buys priced above this (e.g. 0.90 = 90¢) pay almost nothing, so they are
# ignored everywhere: no alerts, and left out of grading. 1 = keep them all.
MAX_BUY_PRICE = 0.90

# Sport-level grading: a play is judged on the wallet's record in THAT sport
# once the sport has MIN_SAMPLE plays at that size. A sport where the wallet
# is losing (SPORT_VETO_PLAYS+ plays, negative ROI) blocks green lights.
SPORT_GRADING = 1
SPORT_VETO_PLAYS = 5

# Skip hedged plays: if a wallet buys BOTH sides of the same market within
# this many hours, neither side is alerted. 0 = off.
HEDGE_HOURS = 48

# Safety cap so one hyperactive wallet can't flood the channel. If a wallet
# has more plays than this in one run, only the largest are posted, plus a
# one-line note saying how many were left out.
MAX_ALERTS_PER_WALLET = 25

# Files (kept next to this script).
BASE_DIR = Path(__file__).resolve().parent
WALLETS_FILE = BASE_DIR / "wallets.txt"
STATE_FILE = BASE_DIR / "state.json"

# ---------------------------------------------------------------------------
# Things you normally don't need to touch
# ---------------------------------------------------------------------------

API_BASE = "https://data-api.polymarket.com"
PAGE_SIZE = 500              # rows per request (the API allows up to 1000)
MAX_PAGES = 40               # per wallet per run
MAX_HISTORY_POSITIONS = 5000 # cap per status on past positions read when grading
MIN_HISTORY_DAYS = 14        # very active wallets: shrink the grading window
                             # (down to this) until it fits under the cap
POSITIONS_PAGE = 1000        # the positions endpoint allows 1000 per page
OVERLAP_SECONDS = 300        # re-check the last 5 minutes each run in case the
                             # API indexed a trade late; duplicates are filtered
GRADER_VERSION = 3           # bumped when grading changes -> everyone re-grades
USER_AGENT = "polymarket-whale-tracker/2.1"   # Discord rejects Python's default

# Size buckets used for grading: (from units, up to units, label)
BUCKETS = [(0, 1, "<1u"), (1, 3, "1-3u"), (3, 5, "3-5u"), (5, float("inf"), "5u+")]

# Event slugs start with the league code, e.g. "nba-cle-nyk-2026-11-11".
LEAGUES = {
    "nba": "NBA", "nfl": "NFL", "nhl": "NHL", "mlb": "MLB", "wnba": "WNBA",
    "cbb": "CBB", "ncaab": "CBB", "cwbb": "CBB", "cfb": "CFB", "ncaaf": "CFB",
    "epl": "Soccer", "lal": "Soccer", "laliga": "Soccer", "es2": "Soccer",
    "sea": "Soccer", "bun": "Soccer", "fl1": "Soccer", "ucl": "Soccer",
    "uel": "Soccer", "uecl": "Soccer", "unl": "Soccer", "mls": "Soccer",
    "fif": "Soccer", "fifa": "Soccer", "fifwc": "Soccer", "wc": "Soccer",
    "bra": "Soccer", "bra2": "Soccer", "arg": "Soccer", "argcopa": "Soccer",
    "por": "Soccer", "ere": "Soccer", "efl": "Soccer", "spl": "Soccer",
    "atp": "Tennis", "wta": "Tennis", "itf": "Tennis", "ufc": "MMA",
    "cs2": "Esports", "lol": "Esports", "dota2": "Esports", "val": "Esports",
}


def sport_of(item):
    """Sport for a play or position, from its event slug (or title)."""
    slug = str(item.get("event_slug") or item.get("eventSlug")
               or item.get("slug") or "").lower()
    prefix = slug.split("-")[0] if slug else ""
    if prefix in LEAGUES:
        return LEAGUES[prefix]
    title = str(item.get("title") or "")
    for word, sport in (("Counter-Strike", "Esports"), ("LoL", "Esports"),
                        ("Dota", "Esports"), ("ATP", "Tennis"), ("WTA", "Tennis"),
                        ("UFC", "MMA")):
        if word in title:
            return sport
    return prefix.upper() or "Other"


# Embed colours
GREEN, RED = 0x2ECC71, 0xE74C3C
GREY, BLUE, GOLD, ORANGE = 0x95A5A6, 0x3498DB, 0xF1C40F, 0xE67E22
PURPLE = 0x9B59B6


# ---------------------------------------------------------------------------
# Settings & wallet list
# ---------------------------------------------------------------------------

def setting(name, default, cast=str):
    """Return the environment variable `name` if set, otherwise `default`."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        sys.exit(f"ERROR: setting {name}={raw!r} is not a valid {cast.__name__}")


def to_number(text):
    return float(text.replace("$", "").replace(",", "").strip())


def parse_wallets(text, default_min):
    """
    Parse the wallet list. One wallet per line, extras separated by commas:

        0xADDRESS
        0xADDRESS, 500                      <- minimum play size $500
        0xADDRESS, 500, BigFish             <- + nickname
        0xADDRESS, BigFish, unit=250        <- + fixed unit size of $250
        0xADDRESS, min=1000, unit=500, name=Sharp Guy
        0xADDRESS, name=EdgeHunter, track=700   <- always post buys $700+

    A bare number is the minimum. `unit=` overrides the calculated unit.
    `track=` posts every buy of that many USDC or more as 👀 TRACKING, even
    when it isn't a green light, and grades it on the scorecard.
    Anything after a # is a comment. Returns a list of dicts.
    """
    wallets, seen = [], set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",") if p.strip()]
        address = parts[0].lower()

        # Basic sanity check: 0x followed by 40 hex characters.
        is_hex = all(c in "0123456789abcdef" for c in address[2:])
        if not (address.startswith("0x") and len(address) == 42 and is_hex):
            print(f"WARNING: wallet list line {line_no} is not a valid "
                  f"0x address - skipped")
            continue
        if address in seen:
            continue
        seen.add(address)

        wallet = {"address": address, "min_usd": default_min,
                  "label": "", "unit_override": None, "track_usd": None}
        for part in parts[1:]:
            try:
                if "=" in part:
                    key, value = (x.strip() for x in part.split("=", 1))
                    key = key.lower()
                    if key == "min":
                        wallet["min_usd"] = to_number(value)
                    elif key == "unit":
                        wallet["unit_override"] = to_number(value)
                    elif key == "track":
                        wallet["track_usd"] = to_number(value)
                    elif key in ("name", "label"):
                        wallet["label"] = value
                    else:
                        print(f"WARNING: line {line_no}: unknown option {key!r}")
                else:
                    wallet["min_usd"] = to_number(part)
            except ValueError:
                if "=" in part:
                    print(f"WARNING: line {line_no}: {part!r} is not a number")
                else:
                    wallet["label"] = part
        wallets.append(wallet)
    return wallets


def load_wallets(default_min):
    """Wallets come from the WALLETS env var (GitHub) or wallets.txt (local)."""
    text = os.environ.get("WALLETS", "").strip()
    if not text and WALLETS_FILE.exists():
        text = WALLETS_FILE.read_text(encoding="utf-8")
    return parse_wallets(text, default_min)


# ---------------------------------------------------------------------------
# State (what we've already posted + each wallet's stats)
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("WARNING: state.json is unreadable - starting fresh")
    return {}


def save_state(state):
    """Write to a temp file first so a crash can't leave a half-written file."""
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def state_key(address):
    """
    Wallets are stored in state.json under a hash rather than the address
    itself, so the file doesn't reveal who you're tracking.
    """
    return hashlib.sha256(address.encode()).hexdigest()[:16]


def trade_id(trade):
    """A fingerprint that identifies one fill, used to avoid re-posting."""
    return "|".join(str(trade.get(k, "")) for k in
                    ("transactionHash", "asset", "side", "size", "price"))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_json(url, payload=None, retries=4):
    """
    GET (or POST when `payload` is given) and return the decoded JSON reply,
    or None for an empty reply. Retries on rate limits (429) and temporary
    server/network errors, then raises RuntimeError.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"

    error = "unknown error"
    for attempt in range(retries):
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body.strip() else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            error = f"HTTP {exc.code}: {body[:200]}"
            if exc.code == 429:
                wait = retry_after_seconds(exc, body)
            elif exc.code >= 500:
                wait = 2 ** attempt
            else:
                break                      # 4xx = our mistake, retry won't help
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = f"network error: {exc}"
            wait = 2 ** attempt
        time.sleep(min(wait, 60))
    raise RuntimeError(error)


def retry_after_seconds(exc, body):
    """Work out how long a 429 reply is asking us to wait."""
    try:
        return float(json.loads(body)["retry_after"]) + 0.25   # Discord style
    except (ValueError, KeyError, TypeError):
        pass
    try:
        return float(exc.headers.get("Retry-After", 5))        # header style
    except (TypeError, ValueError):
        return 5.0


def fetch_pages(path, params, max_rows):
    """
    Read a paged v2 endpoint. Replies look like
        {"data": [...], "pagination": {"next_cursor": "..." or null}}
    and we keep following next_cursor until it's empty or max_rows is hit.
    Returns (rows, more) - `more` is True if rows were left unread.
    """
    rows, cursor = [], None
    for _ in range(MAX_PAGES):
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        reply = http_json(f"{API_BASE}{path}?{urllib.parse.urlencode(query)}")
        if isinstance(reply, dict):
            page = reply.get("data") or []
            cursor = (reply.get("pagination") or {}).get("next_cursor")
        else:                                   # just in case: a bare list
            page, cursor = reply or [], None
        rows.extend(page)
        if not cursor or not page or len(rows) >= max_rows:
            break
    more = bool(cursor and page) or len(rows) > max_rows
    return rows[:max_rows], more


# ---------------------------------------------------------------------------
# Polymarket: trades
# ---------------------------------------------------------------------------

def num(value, default=0.0):
    """Turn an API value into a float (None / junk -> default)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value):
    """Timestamp as whole epoch seconds (tolerates millisecond values)."""
    number = int(num(value))
    return number // 1000 if number > 10**11 else number


def normalize(row):
    """
    Data API v2 names its fields in snake_case (usdc_size, event_slug...).
    Copy the ones we use into the names the rest of this script expects.
    """
    return {
        "type": row.get("type", "TRADE"),
        "timestamp": row.get("timestamp"),
        "side": str(row.get("side", "")).upper(),
        "outcome": row.get("outcome"),
        "size": row.get("size"),
        "price": row.get("price"),
        "title": row.get("title"),
        "slug": row.get("slug"),
        "icon": row.get("icon"),
        "usdcSize": row.get("usdc_size", row.get("usdcSize")),
        "eventSlug": row.get("event_slug", row.get("eventSlug")),
        "transactionHash": row.get("transaction_hash", row.get("transactionHash")),
        "asset": row.get("token_id", row.get("asset")),
        "condition": row.get("condition_id", row.get("conditionId")),
    }


def fetch_trades_since(address, since_ts):
    """Return the wallet's fills with timestamp >= since_ts, oldest first."""
    rows, _ = fetch_pages("/v2/activity", {
        "user": address,
        "type": "TRADE",
        "start": int(since_ts),
        "limit": PAGE_SIZE,
        "sort_direction": "ASC",
    }, max_rows=PAGE_SIZE * MAX_PAGES)

    trades, ids = [], set()
    for row in rows:
        trade = normalize(row)
        if trade["type"] != "TRADE":
            continue
        tid = trade_id(trade)
        if tid not in ids:
            ids.add(tid)
            trades.append(trade)
    trades.sort(key=lambda t: to_int(t.get("timestamp")))
    return trades


def usdc_value(trade):
    """Fill size in USDC. Falls back to shares x price if not supplied."""
    value = num(trade.get("usdcSize"))
    if value <= 0:
        value = num(trade.get("size")) * num(trade.get("price"))
    return value


def combine_fills(trades):
    """
    Merge fills of the same outcome token + side into one "play".
    A $600 buy filled as three $200 pieces becomes one $600 play with an
    average price, the latest time, and the list of fills it came from.
    """
    plays = {}
    for t in trades:
        key = (t.get("asset") or t.get("title"), t.get("outcome"), t.get("side"))
        play = plays.get(key)
        if play is None:
            play = plays[key] = dict(t, usd=0.0, shares=0.0, fills=[])
        play["usd"] += usdc_value(t)
        play["shares"] += num(t.get("size"))
        play["fills"].append(t)
        play["timestamp"] = max(to_int(play["timestamp"]), to_int(t.get("timestamp")))
    for play in plays.values():
        if play["shares"] > 0:
            play["price"] = play["usd"] / play["shares"]
    return sorted(plays.values(), key=lambda p: p["timestamp"])


# ---------------------------------------------------------------------------
# Polymarket: grading past plays
# ---------------------------------------------------------------------------

def bucket_for(units):
    for low, high, label in BUCKETS:
        if low <= units < high:
            return label
    return BUCKETS[-1][2]


def build_profile(wallet, history_days):
    """
    Work out the wallet's unit size and grade its past plays by size.

    Past plays = positions that are finished:
      * CLOSED     - the wallet sold out or redeemed
      * REDEEMABLE - the market resolved but tokens are still held. This
                     matters: whales often never bother redeeming LOSERS, so
                     reading CLOSED alone would hide most of the losses.
    Each position is one graded play:
      stake = shares bought x average entry price
      P&L   = total_pnl (realized + whatever the leftover tokens are worth)
    """
    address = wallet["address"]

    # Very active wallets have thousands of positions. Both lists must cover
    # the SAME time window, or wins and losses get counted over different
    # periods (and the record comes out badly wrong). So: ask the API for
    # the window, and if either list is too long to read in full, halve the
    # window and try again.
    days = history_days
    while True:
        since = int(time.time() - days * 86400)
        rows, capped, oldest_closed = [], False, None
        for status in ("CLOSED", "REDEEMABLE"):
            got, more = fetch_pages("/v2/positions", {
                "user": address,
                "status": status,
                "start": since,             # filters on last activity time
                "sort_by": "TIMESTAMP",
                "sort_direction": "DESC",
                "limit": POSITIONS_PAGE,
            }, max_rows=MAX_HISTORY_POSITIONS)
            capped = capped or more
            if status == "CLOSED" and more and got:
                oldest_closed = min(to_int(p.get("last_event_at")) for p in got)
            rows += got
        if not capped or days <= MIN_HISTORY_DAYS:
            break
        days = max(MIN_HISTORY_DAYS, days // 2)

    # Still too many even at the smallest window: only use the stretch of
    # time the (newest-first) CLOSED list actually reached.
    if oldest_closed:
        since = max(since, oldest_closed)

    plays, seen_tokens = [], set()
    for p in rows:
        token = p.get("token_id") or p.get("asset")
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        when = to_int(p.get("last_event_at")) or to_int(p.get("first_entry_at"))
        if when and when < since:
            continue                        # outside the grading window
        stake = num(p.get("total_size")) * num(p.get("avg_price"))
        if stake <= 0:
            stake = num(p.get("total_cost_usdc")) or num(p.get("entry_cost_usdc"))
        if stake < 1:
            continue                        # dust
        if num(p.get("avg_price")) > MAX_BUY_PRICE:
            continue                        # heavy favourite - not followable
        if p.get("total_pnl") is not None:
            pnl = num(p.get("total_pnl"))
        else:
            pnl = num(p.get("realized_pnl")) + num(p.get("unrealized_pnl"))
        plays.append((stake, pnl, sport_of(p)))

    unit = wallet["unit_override"]
    if not unit and len(plays) >= 5:
        unit = statistics.median(stake for stake, _, _ in plays)

    def empty():
        return {label: {"w": 0, "l": 0, "stake": 0.0, "pnl": 0.0}
                for _, _, label in BUCKETS}
    buckets, sports = empty(), {}
    if unit:
        for stake, pnl, sport in plays:
            size = bucket_for(stake / unit)
            for b in (buckets[size], sports.setdefault(sport, empty())[size]):
                b["w" if pnl > 0 else "l"] += 1
                b["stake"] += stake
                b["pnl"] += pnl
        # Only keep sports with enough plays to matter (keeps state small).
        sports = {sp: bk for sp, bk in sports.items() if sp != "Other"
                  and sum(b["w"] + b["l"] for b in bk.values()) >= SPORT_VETO_PLAYS}

    return {
        "unit": round(unit, 2) if unit else None,
        "unit_override": wallet["unit_override"],
        "graded": len(plays),
        "days": max(1, round((time.time() - since) / 86400)),
        "buckets": buckets,
        "sports": sports,
        "updated": int(time.time()),
        "version": GRADER_VERSION,
    }


def buckets_of(profile, sport=None):
    if sport:
        return ((profile or {}).get("sports") or {}).get(sport) or {}
    return (profile or {}).get("buckets", {})


def bucket_stats(profile, label, sport=None):
    """Return (wins, losses, roi_percent) for one bucket, or None."""
    b = buckets_of(profile, sport).get(label)
    if not b or not b["stake"]:
        return None
    return b["w"], b["l"], 100 * b["pnl"] / b["stake"]


def size_stats(profile, units, sport=None):
    """
    Record for plays of THIS size or bigger, e.g. a 3.2u play is judged on
    every 3u+ play ("3u+ is 10-0"). Plays under 1u use the <1u bucket only.
    Returns (label, (wins, losses, roi)) or (label, None).
    """
    label = bucket_for(units)
    if label == BUCKETS[0][2]:
        return label, bucket_stats(profile, label, sport)
    low = next(lo for lo, _, lab in BUCKETS if lab == label)
    w = l = 0
    stake = pnl = 0.0
    for lo, _, lab in BUCKETS:
        b = buckets_of(profile, sport).get(lab)
        if lo >= low and b:
            w, l = w + b["w"], l + b["l"]
            stake, pnl = stake + b["stake"], pnl + b["pnl"]
    name = f"{low:g}u+"
    return name, ((w, l, 100 * pnl / stake) if stake else None)


def judge(profile, units, sport, cfg):
    """
    The record a play is judged on: (label, (wins, losses, roi)).
      * The wallet's record in THIS sport at this size and up, once that has
        MIN_SAMPLE plays - e.g. "NBA 3u+ is 40-20".
      * If the sport is losing at this size (a few plays, negative ROI), that
        losing record is returned so the play can't get a green light.
      * Otherwise the wallet's all-sports record.
    """
    label, overall = size_stats(profile, units)
    if cfg.get("sport_grading") and sport:
        slabel, sstats = size_stats(profile, units, sport)
        if sstats:
            n = sstats[0] + sstats[1]
            if n >= cfg["min_sample"]:
                return f"{sport} {slabel}", sstats
            if n >= SPORT_VETO_PLAYS and sstats[2] < 0:
                return f"{sport} {slabel}", sstats
    return label, overall


def american_odds(price):
    """Polymarket price (0-1) -> sportsbook odds, e.g. 0.524 -> -110."""
    if not 0 < price < 1:
        return ""
    if price >= 0.5:
        return f"-{round(100 * price / (1 - price))}"
    return f"+{round(100 * (1 - price) / price)}"


def signal_for(stats, min_sample, green_roi, fade_roi):
    """'green', 'fade' or None for a bucket's (wins, losses, roi)."""
    if not stats:
        return None
    wins, losses, roi = stats
    if wins + losses < min_sample:
        return None
    if roi >= green_roi:
        return "green"
    if roi <= fade_roi:
        return "fade"
    return None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def short(address):
    return f"{address[:6]}…{address[-4:]}"


def size_colour(units):
    if units is None or units < 1:
        return GREY
    if units < 3:
        return BLUE
    if units < 5:
        return GOLD
    return ORANGE


def play_signal(play, profile, cfg, wallet=None):
    """'green', 'big', 'fade', 'watch' or None (sells never get a signal)."""
    if play.get("side") != "BUY":
        return None
    unit = (profile or {}).get("unit")
    if not unit:
        return "watch" if is_tracked(play, wallet) else None
    units = play["usd"] / unit
    _, stats = judge(profile, units, sport_of(play), cfg)
    signal = signal_for(stats, cfg["min_sample"], cfg["green_roi"], cfg["fade_roi"])
    if signal is None and is_big_play(units, stats, cfg):
        signal = "big"
    if signal is None and is_tracked(play, wallet):
        signal = "watch"
    return signal


def is_tracked(play, wallet):
    """A buy at or above the wallet's track= amount."""
    track = (wallet or {}).get("track_usd")
    return bool(track) and play.get("side") == "BUY" and play["usd"] >= track


def is_big_play(units, stats, cfg):
    """A big bet at a size the wallet wins at, but on too few plays to prove it."""
    if not cfg.get("big_units") or units < cfg["big_units"] or not stats:
        return False
    wins, losses, roi = stats
    return wins > losses and roi > 0 and wins + losses < cfg["min_sample"]


def build_alert(play, wallet, profile, cfg):
    """
    Turn one play into a compact Discord embed:

        🟢 GREEN LIGHT · DiamondHands 3.2u play!
        3u+ is 10-0 · +51% ROI
        Play: Buccaneers +194  ($1,900 @ 34¢)
        Spread: Bears (-6.5)
        3 fills • Today at 8:14 AM
    """
    address = wallet["address"]
    name = wallet["label"] or short(address)
    side = play.get("side", "")
    unit = (profile or {}).get("unit")
    units = play["usd"] / unit if unit else None

    # The signal only applies to buys - a sell is the wallet exiting.
    signal, stats_line = None, ""
    if units is not None and side == "BUY":
        label, stats = judge(profile, units, sport_of(play), cfg)
        signal = signal_for(stats, cfg["min_sample"], cfg["green_roi"], cfg["fade_roi"])
        if signal is None and is_big_play(units, stats, cfg):
            signal = "big"
        if stats:
            wins, losses, roi = stats
            stats_line = f"**{label} is {wins}-{losses}** · {roi:+.0f}% ROI"
            if wins + losses < cfg["min_sample"]:
                stats_line += " (small sample)"

    if signal is None and is_tracked(play, wallet):
        signal = "watch"
    size_txt = f" {units:.1f}u" if units is not None else ""
    if side == "SELL":
        header, colour = f"↩️ {name} sold{size_txt}", PURPLE
    elif signal == "green":
        header, colour = f"🟢 GREEN LIGHT · {name}{size_txt} play!", GREEN
    elif signal == "watch":
        header, colour = f"👀 TRACKING · {name}{size_txt} play", GREY
    elif signal == "big":
        header, colour = f"🟡 BIG PLAY · {name}{size_txt} play!", GOLD
    elif signal == "fade":
        header, colour = f"🔴 FADE · {name}{size_txt} play", RED
    else:
        header, colour = f"⚪ {name}{size_txt} play", size_colour(units)

    outcome = play.get("outcome") or "?"
    price = num(play.get("price"))
    odds = american_odds(price)
    slug = play.get("eventSlug") or play.get("slug")
    market_url = f"https://polymarket.com/event/{slug}" if slug else None
    title = (play.get("title") or "Unknown market")[:200]

    verb = "Sold" if side == "SELL" else "Play"
    lines = []
    if stats_line:
        lines.append(stats_line)
    lines.append(f"**{verb}: {outcome} {odds}**  (${play['usd']:,.0f} @ {price * 100:.0f}¢)")
    lines.append(f"[{title}]({market_url})" if market_url else title)
    if cfg.get("novig_url") and side == "BUY":
        lines.append(f"🎯 [Bet on Novig]({cfg['novig_url']}) · search \"{outcome}\"")

    fills = len(play["fills"])
    embed = {
        "title": header[:256],
        # Clicking the header opens the market (or the wallet if no market link)
        "url": market_url or f"https://polymarket.com/profile/{address}",
        "description": "\n".join(lines),
        "color": colour,
        # Discord shows this in the footer as "Today at 8:14 AM"
        "timestamp": datetime.fromtimestamp(play["timestamp"], tz=timezone.utc).isoformat(),
    }
    if fills > 1:
        embed["footer"] = {"text": f"{fills} fills"}
    if str(play.get("icon", "")).startswith("http"):
        embed["thumbnail"] = {"url": play["icon"]}
    return embed


def build_report(wallet, profile, cfg):
    """Weekly report card: record and ROI for each size bucket."""
    name = wallet["label"] or short(wallet["address"])
    unit = profile.get("unit")
    if not unit:
        body = (f"Not enough finished plays yet to grade "
                f"({profile.get('graded', 0)} in the last "
                f"{profile.get('days', cfg['history_days'])} days).")
    else:
        rows = ["Size   Record    ROI"]
        for _, _, label in BUCKETS:
            stats = bucket_stats(profile, label)
            if not stats:
                rows.append(f"{label:<6} -         -")
                continue
            wins, losses, roi = stats
            mark = {"green": " 🟢", "fade": " 🔴"}.get(
                signal_for(stats, cfg["min_sample"], cfg["green_roi"], cfg["fade_roi"]), "")
            rows.append(f"{label:<6} {f'{wins}-{losses}':<9} {roi:+.0f}%{mark}")
        how = "set by you" if profile.get("unit_override") else "median bet"
        body = ("```\n" + "\n".join(rows) + "\n```"
                f"1u = **${unit:,.0f}** ({how}) · {profile['graded']} graded plays, "
                f"last {profile.get('days', cfg['history_days'])} days")
        # Sport edges: proven winning (🟢) and losing (🔴) sport + size spots.
        edges = []
        for sport in sorted(profile.get("sports") or {}):
            for units in (1, 3, 5):
                label, stats = size_stats(profile, units, sport)
                sig = signal_for(stats, cfg["min_sample"], cfg["green_roi"], cfg["fade_roi"])
                if sig:
                    wins, losses, roi = stats
                    edges.append((roi, f"{'🟢' if sig == 'green' else '🔴'} {sport} {label} "
                                       f"{wins}-{losses} {roi:+.0f}%"))
        edges.sort(reverse=True)
        best = [e for r, e in edges if r > 0][:3]
        worst = [e for r, e in edges if r < 0][-2:]
        if best or worst:
            body += "\n**Sport edges:** " + " · ".join(best + worst)
    return {
        "title": f"📊 Report card · {name}",
        "url": f"https://polymarket.com/profile/{wallet['address']}",
        "description": body,
        "color": BLUE,
    }


def post_to_discord(webhook_url, embeds=None, content=None):
    """Send one Discord message (maximum 10 embeds per message)."""
    payload = {"username": "Whale Tracker"}
    if embeds:
        payload["embeds"] = embeds
    if content:
        payload["content"] = content
    http_json(webhook_url, payload=payload)
    time.sleep(0.7)                         # stay well under webhook limits


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def refresh_profile_if_due(wallet, entry, cfg):
    """
    Re-grade the wallet if it's never been graded, the weekly refresh is due,
    or you changed its unit= setting. Returns True if it was refreshed.
    """
    profile = entry.get("profile")
    due = (not profile
           or time.time() - profile.get("updated", 0) >= cfg["refresh_days"] * 86400
           or profile.get("unit_override") != wallet["unit_override"]
           or profile.get("version") != GRADER_VERSION)
    if not due:
        return False
    try:
        entry["profile"] = build_profile(wallet, cfg["history_days"])
        return True
    except RuntimeError as exc:
        # Grading is a bonus - if it fails, alerts still go out (with the old
        # stats if we have them) and grading is retried next run.
        print(f"  {short(wallet['address'])}: grading failed - {exc}")
        return False


def find_hedges(entry, plays, cfg):
    """
    Return ids of buys where the wallet has bought ANOTHER outcome of the same
    market within HEDGE_HOURS (in this run or recent runs). Also remembers this
    run's buys so a hedge placed later can still be spotted.
    """
    hours = cfg.get("hedge_hours", 0)
    if not hours:
        return set()
    now = int(time.time())
    recent = [r for r in entry.get("sides", []) if r[2] >= now - hours * 3600]
    for p in plays:
        if p.get("side") == "BUY" and p.get("condition"):
            recent.append([p["condition"], str(p.get("outcome")), int(p["timestamp"])])
    entry["sides"] = recent[-500:]
    outcomes = {}
    for cond, outcome, _ in recent:
        outcomes.setdefault(cond, set()).add(outcome)
    return {id(p) for p in plays
            if p.get("side") == "BUY" and p.get("condition")
            and len(outcomes.get(p["condition"], ())) > 1}


def process_wallet(wallet, state, webhook_url, cfg):
    """
    Check one wallet, post its new plays, update `state`.
    Returns (number posted, report embed or None).
    """
    address = wallet["address"]
    key = state_key(address)
    now = int(time.time())

    first_time = key not in state
    if first_time:
        state[key] = {"last_ts": now - int(cfg["lookback_hours"] * 3600), "seen": []}
    entry = state[key]

    refreshed = refresh_profile_if_due(wallet, entry, cfg)
    report = (build_report(wallet, entry["profile"], cfg)
              if refreshed and cfg["weekly_report"] else None)
    profile = entry.get("profile")

    if first_time and cfg["lookback_hours"] <= 0:
        print(f"  {short(address)}: new wallet - tracking starts now")
        return 0, report

    already_seen = set(entry.get("seen", []))
    since = max(0, int(entry["last_ts"]) - OVERLAP_SECONDS)
    trades = fetch_trades_since(address, since)
    new_trades = [t for t in trades if trade_id(t) not in already_seen]

    # Combine split fills, then apply this wallet's minimum to the whole play.
    all_plays = combine_fills(new_trades)
    hedged = find_hedges(entry, all_plays, cfg)
    plays = [p for p in all_plays
             if p["usd"] >= wallet["min_usd"]
             and id(p) not in hedged
             and not (p.get("side") == "BUY" and num(p.get("price")) > cfg["max_price"])]

    # Every buy is a candidate for a 🔵 official pick (decided after all
    # wallets have been checked, so plays from different wallets can be
    # compared and matched up).
    for p in plays:
        if p.get("side") == "BUY":
            cfg.setdefault("_candidates", []).append((p, wallet, profile, key))

    # Keep only the plays worth acting on (see ALERTS setting).
    found = len(plays)
    if cfg["alerts"] == "green":
        plays = [p for p in plays
                 if play_signal(p, profile, cfg, wallet) in ("green", "big", "watch")]
    elif cfg["alerts"] == "signals":
        plays = [p for p in plays
                 if play_signal(p, profile, cfg, wallet) in ("green", "big", "watch", "fade")]
    filtered_out = found - len(plays)

    # Log why each skipped buy didn't make the cut (shows in the GitHub run log).
    kept = {id(p) for p in plays}
    unit = (profile or {}).get("unit")
    for p in all_plays:
        if id(p) in kept or p.get("side") != "BUY":
            continue
        price = num(p.get("price"))
        if id(p) in hedged:
            why = "HEDGED - wallet bought both sides of this market"
        elif p["usd"] < wallet["min_usd"]:
            why = f"under min ${wallet['min_usd']:,.0f}"
        elif price > cfg["max_price"]:
            why = f"price {price * 100:.0f}¢ above cap"
        elif not unit:
            why = "wallet has no graded history yet"
        else:
            label, stats = judge(profile, p["usd"] / unit, sport_of(p), cfg)
            if not stats:
                why = f"{label}: no record"
            else:
                w, l, roi = stats
                why = f"{label} {w}-{l} {roi:+.0f}%"
                if w + l < cfg["min_sample"]:
                    why += f" (under {cfg['min_sample']} plays)"
                elif roi < cfg["green_roi"]:
                    why += f" (under +{cfg['green_roi']:g}%)"
        size = f" {p['usd'] / unit:.1f}u" if unit else ""
        print(f"      skip: {p.get('outcome')} ${p['usd']:,.0f}{size} "
              f"[{sport_of(p)}] - {why}")
    skipped = 0
    if len(plays) > cfg["max_alerts"]:
        skipped = len(plays) - cfg["max_alerts"]
        plays = sorted(sorted(plays, key=lambda p: p["usd"], reverse=True)
                       [:cfg["max_alerts"]], key=lambda p: p["timestamp"])

    # Post in batches of 10 embeds (Discord's per-message limit).
    posted = 0
    try:
        for i in range(0, len(plays), 10):
            batch = plays[i:i + 10]
            post_to_discord(webhook_url, embeds=[build_alert(p, wallet, profile, cfg)
                                                 for p in batch])
            posted += len(batch)
            for play in batch:
                already_seen.update(trade_id(f) for f in play["fills"])
                record_alert(state, key, wallet, play, profile, cfg)
        if skipped:
            name = wallet["label"] or short(address)
            post_to_discord(webhook_url, content=(
                f"ℹ️ **{name}** made {skipped} more play(s) above "
                f"${wallet['min_usd']:,.0f} this period - only the largest "
                f"{cfg['max_alerts']} are shown."))
    except RuntimeError:
        # Discord failed part-way. Remember only what was actually posted,
        # and leave last_ts alone so the rest is retried on the next run.
        entry["seen"] = sorted(already_seen)
        raise

    # Success: move the bookmark forward. Keep fingerprints only for fills
    # inside the overlap window - that is all the next run needs.
    if trades:
        newest = max(to_int(t.get("timestamp")) for t in trades)
        entry["last_ts"] = max(int(entry["last_ts"]), newest)
        cutoff = entry["last_ts"] - OVERLAP_SECONDS
        entry["seen"] = sorted(trade_id(t) for t in trades
                               if to_int(t.get("timestamp")) >= cutoff)

    unit = (profile or {}).get("unit")
    unit_txt = f", 1u=${unit:,.0f}" if unit else ""
    print(f"  {short(address)}: {len(new_trades)} new fill(s), {posted} play(s) "
          f"posted, {filtered_out} skipped as average "
          f"(min ${wallet['min_usd']:,.0f}{unit_txt})")
    return posted, report


# ---------------------------------------------------------------------------
# Alert scorecard: grade our own alerts and rate every wallet
# ---------------------------------------------------------------------------

def record_alert(state, key, wallet, play, profile, cfg):
    """Remember a posted 🟢 / 🟡 / 🔴 alert so it can be graded later."""
    signal = play_signal(play, profile, cfg, wallet)
    if signal not in ("green", "big", "fade", "watch"):
        return
    unit = (profile or {}).get("unit")
    alerts = state.setdefault("_alerts", [])
    alerts.append({
        "wallet": key,
        "name": wallet["label"] or short(wallet["address"]),
        "signal": signal,
        "token": str(play.get("asset") or ""),
        "outcome": play.get("outcome"),
        "title": play.get("title"),
        "price": round(num(play.get("price")), 4),
        "units": round(play["usd"] / unit, 2) if unit else None,
        "ts": int(play["timestamp"]),
        "result": None,                     # "W" / "L" once settled
        "pnl": None,                        # units won/lost on a 1u bet
    })
    del alerts[:-1000]                      # keep the file small


def settle_price(position):
    """1.0 / 0.0 once a market has settled, otherwise None."""
    price = num(position.get("current_price"), -1)
    if price >= 0.99:
        return 1.0
    if 0 <= price <= 0.01:
        return 0.0
    return None


def grade_alerts(state, wallets, force=False):
    """
    Check unsettled alerts against the wallet's finished positions. When the
    market has settled, grade it as a 1u bet at the alert price:
      win  -> +(1 - price) / price units     loss -> -1 unit
    """
    meta = state.setdefault("_meta", {})
    now = int(time.time())
    if not force and now - meta.get("graded_at", 0) < GRADE_EVERY_HOURS * 3600:
        return
    alerts = state.get("_alerts", [])
    pending = [a for a in alerts if a["result"] is None]
    if not pending:
        meta["graded_at"] = now
        return

    address_of = {state_key(w["address"]): w["address"] for w in wallets}
    by_wallet = {}
    for a in pending:
        by_wallet.setdefault(a["wallet"], []).append(a)

    for key, items in by_wallet.items():
        address = address_of.get(key)
        if not address:
            continue                        # wallet removed - left to expire
        since = min(a["ts"] for a in items) - 3600
        settled = {}
        try:
            for status in ("CLOSED", "REDEEMABLE"):
                rows, _ = fetch_pages("/v2/positions", {
                    "user": address, "status": status, "start": since,
                    "sort_by": "TIMESTAMP", "sort_direction": "DESC",
                    "limit": POSITIONS_PAGE,
                }, max_rows=MAX_HISTORY_POSITIONS)
                for p in rows:
                    price = settle_price(p)
                    if price is not None:
                        settled[str(p.get("token_id") or p.get("asset"))] = price
        except RuntimeError as exc:
            print(f"  grading {short(address)} failed - {exc}")
            continue
        for a in items:
            final = settled.get(a["token"])
            if final is None or not 0 < a["price"] < 1:
                continue
            if final == 1.0:
                a["result"], a["pnl"] = "W", round((1 - a["price"]) / a["price"], 3)
            else:
                a["result"], a["pnl"] = "L", -1.0

    # Forget alerts that never settled (market voided, wallet removed...).
    cutoff = now - ALERT_EXPIRE_DAYS * 86400
    state["_alerts"] = [a for a in alerts if a["result"] or a["ts"] >= cutoff]
    meta["graded_at"] = now


def tally(alerts):
    """(wins, losses, roi %, units) for a list of graded alerts."""
    graded = [a for a in alerts if a["result"]]
    wins = sum(1 for a in graded if a["result"] == "W")
    units = sum(a["pnl"] for a in graded)
    roi = 100 * units / len(graded) if graded else 0.0
    return wins, len(graded) - wins, roi, units


def tally_line(label, alerts):
    wins, losses, roi, units = tally(alerts)
    if wins + losses == 0:
        return f"{label}: no settled plays yet"
    return f"{label}: **{wins}-{losses}** · {roi:+.0f}% ROI · **{units:+.2f}u**"


def best_play_type(profile, cfg):
    """
    What to follow this wallet on: the sport + size (or all-sports size) with
    the best ROI that has a proven edge, else a winning big-play size that's
    still a small sample. Returns (text, roi) or None.
    """
    if not (profile or {}).get("unit"):
        return None
    scopes = [None] + (sorted((profile.get("sports") or {}).keys())
                       if cfg.get("sport_grading") else [])
    best = None
    for sport in scopes:
        for units in (1, 3, 5):
            label, stats = size_stats(profile, units, sport)
            if not stats:
                continue
            wins, losses, roi = stats
            if wins + losses >= cfg["min_sample"] and roi >= cfg["green_roi"]:
                name = f"{sport} {label}" if sport else label
                if not best or roi > best[1]:
                    best = (f"{name} plays ({wins}-{losses}, {roi:+.0f}%)", roi)
    if best:
        return best
    for units in (3, 5):
        label, stats = size_stats(profile, units)
        if stats and is_big_play(units, stats, cfg):
            wins, losses, roi = stats
            return (f"🟡 big plays {label} ({wins}-{losses}, small sample)", roi)
    return None


def build_scorecard(state, wallets, cfg):
    """The weekly scorecard embed."""
    alerts = state.get("_alerts", [])
    week_ago = int(time.time()) - 7 * 86400
    recent = [a for a in alerts if a["ts"] >= week_ago]

    lines = ["**Last 7 days**",
             tally_line("🔵 Whale Model", [a for a in recent if a["signal"] == "official"]),
             tally_line("🟢 Green lights", [a for a in recent if a["signal"] == "green"]),
             tally_line("🟡 Big plays", [a for a in recent if a["signal"] == "big"]),
             "",
             "**Since tracking began**",
             tally_line("🔵 Whale Model", [a for a in alerts if a["signal"] == "official"]),
             tally_line("🟢 Green lights", [a for a in alerts if a["signal"] == "green"]),
             tally_line("🟡 Big plays", [a for a in alerts if a["signal"] == "big"]),
             ]
    if any(a["signal"] == "watch" for a in alerts):      # only if track= is used
        lines.insert(4, tally_line("👀 Tracking", [a for a in recent if a["signal"] == "watch"]))
        lines.append(tally_line("👀 Tracking", [a for a in alerts if a["signal"] == "watch"]))

    # Wallet table: how each wallet's alerts have done (all-time).
    rows = []
    for w in wallets:
        key = state_key(w["address"])
        name = (w["label"] or short(w["address"]))[:15]
        mine = [a for a in alerts if a["wallet"] == key
                and a["signal"] in ("green", "big", "watch")]
        wins, losses, roi, units = tally(mine)
        rows.append((units if wins + losses else -999, name, wins, losses, roi, units))
    rows.sort(key=lambda r: r[0], reverse=True)
    table = ["Wallet          Record  ROI    Units"]
    for _, name, wins, losses, roi, units in rows:
        if wins + losses:
            table.append(f"{name:<15} {f'{wins}-{losses}':<7} {roi:>+4.0f}%  {units:>+6.2f}u")
        else:
            table.append(f"{name:<15} -")
    lines += ["", "**Wallets** (their 🟢/🟡 alerts)", "```", *table, "```"]

    # Focus list: each wallet's strongest play type, best first.
    focus = []
    for w in wallets:
        key = state_key(w["address"])
        found = best_play_type((state.get(key) or {}).get("profile"), cfg)
        if found:
            focus.append((found[1], w["label"] or short(w["address"]), found[0]))
    focus.sort(reverse=True)
    lines.append("**🎯 Focus right now**")
    if focus:
        lines += [f"• **{name}** — {text}" for _, name, text in focus[:6]]
    else:
        lines.append("Nothing proven yet - keep tracking.")

    pending = sum(1 for a in alerts if a["result"] is None)
    return {
        "title": "📈 Alert scorecard",
        "description": "\n".join(lines)[:4000],
        "color": GOLD,
        "footer": {"text": f"Graded as a flat 1u bet per alert · {pending} alert(s) still pending"},
    }


def scorecard_due(state, cfg):
    days = cfg.get("scorecard_days", SCORECARD_DAYS)
    if not days:
        return False
    meta = state.setdefault("_meta", {})
    if "scorecard_at" not in meta:          # start the clock on first run
        meta["scorecard_at"] = int(time.time())
        return False
    return time.time() - meta["scorecard_at"] >= days * 86400


# ---------------------------------------------------------------------------
# 🔵 Whale Model official picks
# ---------------------------------------------------------------------------

def safe_roi(stats):
    """ROI minus its luck range (about ±200% / sqrt(plays))."""
    if not stats:
        return None
    wins, losses, roi = stats
    n = wins + losses
    return roi - 200 / math.sqrt(n) if n else None


def wallet_is_cold(state, key):
    """True if this wallet's graded alerts are losing over a fair sample."""
    mine = [a for a in state.get("_alerts", [])
            if a["wallet"] == key and a["signal"] != "official"]
    wins, losses, _, units = tally(mine)
    return wins + losses >= COLD_AFTER and units < 0


def pick_officials(state, cfg):
    """
    Choose this run's 🔵 picks from every wallet's new buys.
    A play qualifies if EITHER
      * proven edge: a 2u+ bet at a size where the wallet's ROI, minus the
        luck range, is still +5% or better, and the wallet isn't cold; OR
      * consensus: 2+ of your wallets bought the same side within 24 hours.
    Each market side becomes an official pick only once.
    """
    now = int(time.time())
    candidates = cfg.pop("_candidates", [])
    recent = state.setdefault("_recent_buys", [])
    for play, wallet, profile, key in candidates:
        unit = (profile or {}).get("unit")
        recent.append({"wallet": key, "name": wallet["label"] or short(wallet["address"]),
                       "token": str(play.get("asset") or ""), "ts": int(play["timestamp"]),
                       "units": round(play["usd"] / unit, 1) if unit else None})
    keep_after = now - 2 * cfg["consensus_hours"] * 3600
    state["_recent_buys"] = recent = [r for r in recent if r["ts"] >= keep_after]
    done = set(state.setdefault("_official_tokens", []))

    picks = []
    for play, wallet, profile, key in candidates:
        token = str(play.get("asset") or "")
        if not token or token in done:
            continue
        unit = (profile or {}).get("unit")
        units = play["usd"] / unit if unit else None
        label, stats = (judge(profile, units, sport_of(play), cfg) if units
                        else (None, None))

        window = play["timestamp"] - cfg["consensus_hours"] * 3600
        others = {}
        for r in recent:
            if r["token"] == token and r["wallet"] != key and r["ts"] >= window:
                others[r["wallet"]] = r
        consensus = len(others) >= 1

        sr = safe_roi(stats)
        proven = (units is not None and units >= cfg["official_units"]
                  and sr is not None and sr >= cfg["official_safe_roi"]
                  and not wallet_is_cold(state, key))
        if not (proven or consensus):
            continue
        done.add(token)
        picks.append({"play": play, "wallet": wallet, "key": key, "units": units,
                      "label": label, "stats": stats, "others": list(others.values()),
                      "why": "proven edge" if proven else "consensus"})
    state["_official_tokens"] = sorted(done)[-2000:]
    return picks


def build_official(pick):
    play, wallet = pick["play"], pick["wallet"]
    name = wallet["label"] or short(wallet["address"])
    price = num(play.get("price"))
    slug = play.get("eventSlug") or play.get("slug")
    market_url = f"https://polymarket.com/event/{slug}" if slug else None
    title = (play.get("title") or "Unknown market")[:200]

    who = f"**{name}**"
    if pick["units"] is not None:
        who += f" {pick['units']:.1f}u"
    if pick["stats"]:
        wins, losses, roi = pick["stats"]
        who += f" · {pick['label']} is {wins}-{losses} · {roi:+.0f}% ROI"
    lines = [f"**Play: {play.get('outcome') or '?'} {american_odds(price)}**  "
             f"(${play['usd']:,.0f} @ {price * 100:.0f}¢)",
             who]
    for o in pick["others"]:
        size = f" ({o['units']}u)" if o.get("units") else ""
        lines.append(f"+ **{o['name']}** also on it{size}")
    lines.append(f"[{title}]({market_url})" if market_url else title)
    why = ("Proven edge at this bet size" if pick["why"] == "proven edge"
           else f"{len(pick['others']) + 1} of your wallets on the same side")
    return {
        "title": "🔵 WHALE MODEL PICK",
        "url": market_url or f"https://polymarket.com/profile/{wallet['address']}",
        "description": "\n".join(lines),
        "color": BLUE,
        "footer": {"text": why},
        "timestamp": datetime.fromtimestamp(play["timestamp"], tz=timezone.utc).isoformat(),
    }


def record_official(state, pick):
    play = pick["play"]
    state.setdefault("_alerts", []).append({
        "wallet": pick["key"],
        "name": pick["wallet"]["label"] or short(pick["wallet"]["address"]),
        "signal": "official",
        "token": str(play.get("asset") or ""),
        "outcome": play.get("outcome"),
        "title": play.get("title"),
        "price": round(num(play.get("price")), 4),
        "units": round(pick["units"], 2) if pick["units"] else None,
        "ts": int(play["timestamp"]),
        "result": None,
        "pnl": None,
    })


def send_preview(webhook_url, cfg):
    """
    python tracker.py --preview
    Posts a made-up GREEN LIGHT alert and a made-up weekly scorecard so you
    can see the layouts. Reads no wallets and doesn't touch state.json.
    """
    now = int(time.time())
    fills = [{"usdcSize": 1400}, {"usdcSize": 2100}, {"usdcSize": 1662}]
    play = {"side": "BUY", "outcome": "Thunder", "price": 0.35, "usd": 5162.0,
            "title": "Thunder vs. Trail Blazers", "eventSlug": "",
            "timestamp": now, "fills": fills}
    profile = {"unit": 2450, "buckets": {
        "<1u": {"w": 30, "l": 28, "stake": 40000, "pnl": 1200},
        "1-3u": {"w": 41, "l": 26, "stake": 160000, "pnl": 38000},
        "3-5u": {"w": 12, "l": 8, "stake": 90000, "pnl": 19000},
        "5u+": {"w": 6, "l": 4, "stake": 70000, "pnl": 9000}}}
    wallet = {"address": "0x" + "0" * 40, "label": "PreviewWhale"}
    preview_cfg = dict(cfg, min_sample=15)
    embed = build_alert(play, wallet, profile, preview_cfg)
    embed["footer"] = {"text": "PREVIEW - not a real play"}
    post_to_discord(webhook_url, embeds=[embed])

    # A sample weekly scorecard with made-up wallets and results.
    names = {"TrustMeBro": [("green", "W", .52), ("green", "W", .48), ("green", "L", .55),
                            ("green", "W", .40), ("big", "W", .35), ("green", "L", .60)],
             "DiamondHands": [("big", "W", .58), ("big", "W", .45), ("green", "L", .50),
                              ("big", "W", .62)],
             "ShadowTrader": [("green", "L", .47), ("green", "W", .30), ("green", "L", .52)],
             "Fvalp": []}
    def fp(b):   # build a fake profile from {bucket: (wins, losses, stake, pnl)}
        return {"unit": 2000, "buckets": {
            lab: dict(zip(("w", "l", "stake", "pnl"), b.get(lab, (0, 0, 0.0, 0.0))))
            for _, _, lab in BUCKETS}}
    fake_profiles = [
        fp({"<1u": (53, 43, 50000, 10500), "1-3u": (41, 26, 120000, 32400)}),
        fp({"<1u": (23, 29, 30000, -8700), "3-5u": (7, 0, 50000, 26000), "5u+": (3, 0, 40000, 20000)}),
        fp({"1-3u": (56, 23, 150000, 64500), "5u+": (105, 43, 900000, 18000)}),
        fp({"<1u": (20, 22, 20000, -500)})]
    fake_wallets, fake_state, alerts = [], {"_alerts": []}, []
    for i, (name, results) in enumerate(names.items()):
        address = "0x" + str(i + 1) * 40
        key = state_key(address)
        fake_wallets.append({"address": address, "label": name})
        fake_state[key] = {"profile": fake_profiles[i]}
        for signal, result, price in results:
            alerts.append({"wallet": key, "signal": signal, "ts": now - 86400,
                           "result": result, "price": price,
                           "pnl": round((1 - price) / price, 3) if result == "W" else -1.0})
    for result, price in [("W", .45), ("W", .52), ("L", .50), ("W", .38)]:
        alerts.append({"wallet": state_key(fake_wallets[0]["address"]), "signal": "official",
                       "ts": now - 86400, "result": result, "price": price,
                       "pnl": round((1 - price) / price, 3) if result == "W" else -1.0})
    alerts.append({"wallet": state_key(fake_wallets[0]["address"]), "signal": "green",
                   "ts": now, "result": None, "price": .5, "pnl": None})
    fake_state["_alerts"] = alerts
    official = build_official({
        "play": dict(play, outcome="Thunder"), "wallet": {"address": wallet["address"],
                                                          "label": "TrustMeBro"},
        "units": 2.1, "label": "1u+", "stats": (59, 38, 21.0),
        "others": [{"name": "BagHolder", "units": 1.8}], "why": "proven edge"})
    official["footer"] = {"text": "PREVIEW - not a real pick"}
    post_to_discord(setting("OFFICIAL_WEBHOOK_URL", OFFICIAL_WEBHOOK_URL) or webhook_url,
                    embeds=[official])

    card = build_scorecard(fake_state, fake_wallets, preview_cfg)
    card["footer"] = {"text": "PREVIEW - made-up numbers"}
    post_to_discord(webhook_url, embeds=[card])
    print("Preview alert, official pick and scorecard posted to Discord.")


def main():
    webhook_url = setting("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL)
    default_min = setting("DEFAULT_MIN_TRADE_USD", DEFAULT_MIN_TRADE_USD, float)
    cfg = {
        "lookback_hours": setting("FIRST_RUN_LOOKBACK_HOURS", FIRST_RUN_LOOKBACK_HOURS, float),
        "max_alerts": setting("MAX_ALERTS_PER_WALLET", MAX_ALERTS_PER_WALLET, int),
        "history_days": setting("HISTORY_DAYS", HISTORY_DAYS, int),
        "min_sample": setting("MIN_SAMPLE", MIN_SAMPLE, int),
        "green_roi": setting("GREEN_LIGHT_MIN_ROI", GREEN_LIGHT_MIN_ROI, float),
        "fade_roi": setting("FADE_MAX_ROI", FADE_MAX_ROI, float),
        "refresh_days": setting("PROFILE_REFRESH_DAYS", PROFILE_REFRESH_DAYS, float),
        "weekly_report": setting("WEEKLY_REPORT", WEEKLY_REPORT, int),
        "alerts": setting("ALERTS", ALERTS).lower(),
        "novig_url": setting("NOVIG_URL", NOVIG_URL),
        "big_units": setting("BIG_PLAY_UNITS", BIG_PLAY_UNITS, float),
        "scorecard_days": setting("SCORECARD_DAYS", SCORECARD_DAYS, float),
        "max_price": setting("MAX_BUY_PRICE", MAX_BUY_PRICE, float),
        "sport_grading": setting("SPORT_GRADING", SPORT_GRADING, int),
        "hedge_hours": setting("HEDGE_HOURS", HEDGE_HOURS, float),
        "official": setting("OFFICIAL_PICKS", OFFICIAL_PICKS, int),
        "official_safe_roi": setting("OFFICIAL_MIN_SAFE_ROI", OFFICIAL_MIN_SAFE_ROI, float),
        "official_units": setting("OFFICIAL_MIN_UNITS", OFFICIAL_MIN_UNITS, float),
        "consensus_hours": setting("CONSENSUS_HOURS", CONSENSUS_HOURS, float),
    }
    official_url = setting("OFFICIAL_WEBHOOK_URL", OFFICIAL_WEBHOOK_URL) or webhook_url
    if cfg["alerts"] not in ("green", "signals", "all"):
        sys.exit("ERROR: ALERTS must be green, signals or all")
    if not webhook_url.startswith("https://"):
        sys.exit("ERROR: no Discord webhook. Set DISCORD_WEBHOOK_URL at the top "
                 "of tracker.py (local) or as a GitHub secret.")
    if "--preview" in sys.argv:
        send_preview(webhook_url, cfg)
        return

    wallets = load_wallets(default_min)
    if not wallets:
        sys.exit("ERROR: no wallets. Add them to wallets.txt (local) or the "
                 "WALLETS GitHub secret.")

    state = load_state()

    # python tracker.py --scorecard  ->  grade now and post the scorecard now
    if "--scorecard" in sys.argv:
        grade_alerts(state, wallets, force=True)
        post_to_discord(webhook_url, embeds=[build_scorecard(state, wallets, cfg)])
        save_state(state)
        print("Scorecard posted to Discord.")
        return

    print(f"Checking {len(wallets)} wallet(s)...")
    total_posted, failures, reports = 0, 0, []

    for wallet in wallets:
        try:
            posted, report = process_wallet(wallet, state, webhook_url, cfg)
            total_posted += posted
            if report:
                reports.append(report)
        except RuntimeError as exc:
            # One bad wallet shouldn't stop the others. Its bookmark is not
            # advanced, so its trades are picked up next run.
            failures += 1
            print(f"  {short(wallet['address'])}: FAILED - {exc}")
        save_state(state)                   # save progress after every wallet
        time.sleep(0.3)                     # be polite to the API

    # Report cards go out together at the end, 10 per message.
    try:
        for i in range(0, len(reports), 10):
            post_to_discord(webhook_url, embeds=reports[i:i + 10])
    except RuntimeError as exc:
        print(f"Report cards failed to post - {exc}")

    # 🔵 Whale Model official picks: the best of this run's buys, all wallets.
    if cfg["official"]:
        picks = pick_officials(state, cfg)
        try:
            for i in range(0, len(picks), 10):
                batch = picks[i:i + 10]
                post_to_discord(official_url, embeds=[build_official(p) for p in batch])
                for p in batch:
                    record_official(state, p)
            if picks:
                print(f"{len(picks)} official pick(s) posted.")
        except RuntimeError as exc:
            print(f"Official picks failed to post - {exc}")
    cfg.pop("_candidates", None)

    # Grade alerts whose games have settled; post the weekly scorecard.
    grade_alerts(state, wallets)
    if scorecard_due(state, cfg):
        try:
            post_to_discord(webhook_url, embeds=[build_scorecard(state, wallets, cfg)])
            state["_meta"]["scorecard_at"] = int(time.time())
            print("Weekly scorecard posted.")
        except RuntimeError as exc:
            print(f"Scorecard failed to post - {exc}")
    save_state(state)

    print(f"Done. {total_posted} play(s) posted, {len(reports)} report card(s), "
          f"{failures} wallet(s) failed.")
    if failures:
        sys.exit(1)                         # shows the run as failed in GitHub


if __name__ == "__main__":
    main()
