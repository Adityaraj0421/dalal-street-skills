#!/usr/bin/env python3
"""Time Machine for nse-timemachine. Standard library only. Python 3.8+.

Replays a planned NSE/BSE trade before you risk a rupee: 10,000 simulated runs
of the rest of the session from your entry, plus the same rule on every past
session in the data. Results are in R, and R includes the round-trip charges.

    python3 timemachine.py run                          # nse/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save nse/timemachine.json
    python3 timemachine.py run --until 15:05            # stop each replay earlier than the plan's flat_by
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol RELIANCE --date 2026-10-26 --entry 1193.60 \\
        --stop 1183.65 --target 1203.55 --entry-time 10:00
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from nse/timemachine.json

How the replays work (the US Time Machine's algorithm, same random draws):
  1. Fetch 60 days of 5-minute bars (Yahoo v8 chart, range=60d interval=5m;
     one request). Keep only FULL sessions before the plan's date: 72 bars,
     09:15 to 15:15 IST. Yahoo's NSE 5-minute data does not carry the 15:20
     and 15:25 bars (null every day), so 15:15 is where replays end. Muhurat
     or other short sessions, gaps and today are left out.
  2. The pool: for every bar slot 1..71 of the day, each past session's bar as
     ratios to the previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept in
     1..71, then one past bar from pool[j] (random.choice), and scales it to
     the current price. The stop is checked before the target in every bar,
     so a bar that touches both counts as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at the exit time. For a MIS plan the exit
     time is the plan's flat_by (default 15:10): the position is squared off
     there, win or lose. For a CNC (delivery) plan the replay runs to the
     end of today's data (15:15; the market closes 15:30) only: the intraday replay does NOT model overnight holding,
     gaps or T+1, so it says nothing about how a delivery trade does after
     today.
  5. R: 1R is the MAX LOSS of the trade = (entry - stop) x qty + the round-trip
     charges if the stop is hit (STT, brokerage, exchange, SEBI, stamp, GST,
     from trade_charges()). A target nets (target - entry) x qty minus its
     charges, so a "1R" target is worth a little less than +1R. A stop is
     exactly -1R (a gap through the stop still counts -1R; a real fill can be
     worse). Anything else is net P&L at the exit price (charges included)
     over that 1R. "green" means finished in profit after charges. The
     quantity is the plan's, or a reference position of about Rs 1,00,000
     when the plan has none (charges depend on size: nse-budget sizes the
     real one).
  6. The price at the exit time: median and the 5th to 95th percentile of all
     replays (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a
     second run of 2,000 paths with the same seed. The histogram has 24 bins
     between the 1st and 99th percentile.
  7. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R, prices rounded to the 0.05 tick)
     re-run on every one of those past full sessions, first close beyond the
     range = entry, same stop-first rule, same charges.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even at the target's NET R: 1 / (1 + R). The gap, in points:
  less than 5 points    "Close to a coin flip" (1R plan) / "Close to break-even"
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither, and the
average R when its sign goes against the lean.

5-minute bars only. The run refuses with a plain message when there are fewer
than 20 full sessions, when the entry bar is not in the data, or when the
plan's day is not an NSE trading day. Same plan, same data and same --seed
give the same numbers on any machine. A simulation built from the stock's own
recent bars is not a prediction: results, news, circuits and halts can do what
the last 60 days never did.

Data: https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m
(unofficial and free, can lag; falls back to query1 on 429 or 5xx).
--offline FILE reads a saved chart JSON (.json or .json.gz) instead.
All times are IST; --now "2026-10-26 10:30" pins the clock.
"""

import argparse
import gzip
import math
import random
import statistics

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; india-trader-skill/1.0)"
HOSTS = ("query2", "query1")
BACKOFF = 5.0
EPOCH = datetime.datetime(1970, 1, 1)


class IndiaError(Exception):
    pass


# ---------------------------------------------------------------- console

def utf8_stdout():
    """Windows consoles default to cp1252, which cannot print the rupee sign.
    Call once at the top of main()."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ---------------------------------------------------------------- time (IST)
# India has no daylight saving: IST is always UTC+5:30.

IST_OFFSET = datetime.timedelta(hours=5, minutes=30)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def to_ist(utc):
    return utc.replace(tzinfo=None) + IST_OFFSET


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST_OFFSET


def utc_of(ts):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    """--now -> naive UTC. "2026-10-19 10:15" (or ...T10:15, or just 10:15 for
    today) is India time (IST); an offset or Z makes it absolute."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_ist(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise IndiaError('--now wants an India time like "2026-10-19 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise IndiaError("%s wants an India time like 14:45, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def short(t):
    return "%d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


# ---------------------------------------------------------------- NSE calendar
# Equity (capital market) segment. NSE and BSE share these holidays.
# Source: NSE circular NSE/CMTR/71775 (Dec 12, 2025) plus NSE/CMTR/72260
# (Jan 12, 2026, adds Jan 15). Read from nsearchives.nseindia.com on 2026-10-26.
# NSE publishes next year's list in December: add it to nse/holidays.json
# (see load_calendar) until this table is updated.

NSE_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election in Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
# Special sessions on otherwise-closed days, e.g. Muhurat trading on Diwali.
# 2026: Sunday 2026-11-08, normal market 14:20-15:30 (special pre-open
# 14:05-14:20), per NSE's circular of 2026-10-19 as reported by the press
# (BusinessToday, Zee Business, Upstox, 2026-10-19). Add later years to
# nse/holidays.json "special_sessions".
SPECIAL_SESSIONS = {
    "2026-11-08": {"open": datetime.time(14, 20), "close": datetime.time(15, 30), "name": "Diwali Muhurat trading"},
}
CALENDAR_YEARS = (2026,)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)

_calendar_loaded = False


def load_calendar(path="nse/holidays.json"):
    """Merge the user's overrides into the built-in calendar, once:
    {"holidays": {"2027-01-26": "Republic Day"},
     "special_sessions": {"2026-11-08": {"open": "18:00", "close": "19:00", "name": "Muhurat trading"}},
     "years": [2027]}"""
    global _calendar_loaded, CALENDAR_YEARS
    if _calendar_loaded:
        return
    _calendar_loaded = True
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise IndiaError("%s is not valid JSON: %s" % (path, e))
    for d, name in (doc.get("holidays") or {}).items():
        NSE_HOLIDAYS[str(d)] = str(name)
    for d, s in (doc.get("special_sessions") or {}).items():
        SPECIAL_SESSIONS[str(d)] = {"open": parse_hhmm(s["open"], "special session open"),
                                    "close": parse_hhmm(s["close"], "special session close"),
                                    "name": s.get("name") or "special session"}
    years = set(CALENDAR_YEARS) | set(int(y) for y in (doc.get("years") or []))
    CALENDAR_YEARS = tuple(sorted(years))


def trading_day(d):
    """(is_trading_day, reason). Weekends, NSE holidays, special sessions."""
    k = d.isoformat()
    if k in SPECIAL_SESSIONS:
        return True, ""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(k)
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def session(d):
    """(open_time, close_time) in IST, or None if closed all day."""
    s = SPECIAL_SESSIONS.get(d.isoformat())
    if s:
        return s["open"], s["close"]
    ok, _ = trading_day(d)
    return (OPEN, CLOSE) if ok else None


def next_open(d):
    nd = d + datetime.timedelta(days=1)
    for _ in range(15):
        s = session(nd)
        if s:
            return "%s %s" % (nd.isoformat(), hm(s[0]))
        nd += datetime.timedelta(days=1)
    return None


# ---------------------------------------------------------------- closing auction (CAS)
# From 2026-08-03 F&O stocks ("CAS stocks") close through a Closing Auction
# Session: continuous trading in them ends at 15:15, the auction runs to about
# 15:35 (NSE/CMTR/73362, go-live NSE/CMTR/75479). Other stocks still trade to
# 15:30. SEBI's circular of 2026-10-15 moves the auction to 15:30-15:40 from
# 2026-12-07, so continuous trading runs to 15:30 for every stock again.
# Stop-loss orders are not accepted during the auction: plan to be out of an
# intraday position before continuous trading ends.
# Which stocks are CAS stocks: the F&O underlyings in NSE's open file
# https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv (cache it daily).

CAS_PERIODS = (
    # (from, to inclusive or None, continuous-trading end for CAS stocks)
    ("2026-08-03", "2026-12-06", datetime.time(15, 15)),
)
FO_LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
FO_INDEXES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def parse_fo_symbols(text):
    """fo_mktlots.csv -> set of F&O stock symbols (indexes left out)."""
    out = set()
    for i, line in enumerate(text.splitlines()):
        cells = [c.strip() for c in line.split(",")]
        if i == 0 or len(cells) < 2 or not cells[1] or cells[1].upper() == "SYMBOL":
            continue
        s = cells[1].upper()
        if s not in FO_INDEXES:
            out.add(s)
    return out


def continuous_close(d, cas_stock):
    """When continuous trading ends on day d for this stock (IST time), or
    None if the market is closed. A special session (Muhurat) keeps its own
    close; a CAS stock on such a day ends 15 minutes before it while the
    15:15 rule is in force."""
    s = session(d)
    if not s:
        return None
    close = s[1]
    if not cas_stock:
        return close
    k = d.isoformat()
    for start, end, t in CAS_PERIODS:
        if k >= start and (end is None or k <= end):
            if k in SPECIAL_SESSIONS:
                c = datetime.datetime.combine(d, close) - datetime.timedelta(minutes=15)
                return c.time()
            return min(close, t)
    return close


def calendar_note(d):
    if d.year in CALENDAR_YEARS:
        return None
    return ("The holiday list covers %s only: check nseindia.com's holiday page for %s and add it to "
            "nse/holidays.json." % (" and ".join(map(str, CALENDAR_YEARS)), d.year))


# ---------------------------------------------------------------- symbols
# Users type RELIANCE, RELIANCE.NS (NSE) or RELIANCE.BO (BSE); bare = NSE.
# Yahoo keeps the dot (TCS.NS) and needs & and ^ escaped in the URL.

INDEX_ALIASES = {
    "NIFTY": "^NSEI", "NIFTY50": "^NSEI", "NIFTY 50": "^NSEI",
    "BANKNIFTY": "^NSEBANK", "NIFTYBANK": "^NSEBANK",
    "SENSEX": "^BSESN", "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCAP": "^NSEMDCP50", "NIFTYIT": "^CNXIT", "INDIAVIX": "^INDIAVIX",
}


def parse_symbol(text, default_exchange="NSE"):
    """-> {"yahoo": "RELIANCE.NS", "exchange": "NSE", "tradingsymbol": "RELIANCE", "index": False}"""
    s = str(text).strip().upper()
    if not s:
        raise IndiaError("empty ticker")
    if s in INDEX_ALIASES or s.startswith("^"):
        y = INDEX_ALIASES.get(s, s)
        return {"yahoo": y, "exchange": "INDEX", "tradingsymbol": s, "index": True}
    if s.endswith(".NS"):
        return {"yahoo": s, "exchange": "NSE", "tradingsymbol": s[:-3], "index": False}
    if s.endswith(".BO"):
        return {"yahoo": s, "exchange": "BSE", "tradingsymbol": s[:-3], "index": False}
    if s.startswith("NSE:"):
        return parse_symbol(s[4:] + ".NS")
    if s.startswith("BSE:"):
        return parse_symbol(s[4:] + ".BO")
    if not re.match(r"^[A-Z0-9&\-_]+$", s):
        raise IndiaError("%r is not an NSE/BSE ticker (try RELIANCE, TCS.NS or RELIANCE.BO)" % text)
    return parse_symbol(s + (".BO" if default_exchange == "BSE" else ".NS"))


def yahoo_quote(ysym):
    return urllib.parse.quote(ysym, safe="._-")


# ---------------------------------------------------------------- Yahoo

def _open_url(url, timeout=20, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """GET a Yahoo path: query2 first, query1 when query2 answers 429 or 5xx.
    An HTTP 429 gets one retry on the same host after BACKOFF seconds.
    A 404 body is returned: Yahoo says why (an unknown ticker) in it."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise IndiaError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    time.sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise IndiaError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise IndiaError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def chart_path(ysym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (yahoo_quote(ysym), rng, interval)


# ---------------------------------------------------------------- money
# Rupees with Indian digit grouping: 12,34,567.89. Use "Rs" where the
# output may land somewhere without UTF-8.

RUPEE = "₹"


def group_in(n):
    """1234567 -> '12,34,567'."""
    s = str(int(n))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def inr(x, dp=2, sign=False):
    if x is None:
        return "-"
    neg = x < 0
    v = round(abs(float(x)), dp)
    whole = int(v)
    frac = ("%.*f" % (dp, v - whole))[1:] if dp else ""
    out = RUPEE + group_in(whole) + frac
    if neg:
        return "-" + out
    return ("+" + out) if sign else out


def lakh_crore(x):
    """Big amounts the Indian way: 3.45 lakh, 12.30 crore."""
    a = abs(float(x))
    if a >= 1e7:
        return "%s%.2f crore" % ("-" if x < 0 else "", a / 1e7)
    if a >= 1e5:
        return "%s%.2f lakh" % ("-" if x < 0 else "", a / 1e5)
    return inr(x, 0)


# ---------------------------------------------------------------- ticks
# NSE tick sizes are 0.05 for most stocks, 0.01 for some low-priced ones.
# 0.05 multiples are valid either way, so rounding to 0.05 is always safe.

TICK = 0.05


def _q(x, tick):
    return round(x / tick, 6)


def tick_down(x, tick=TICK):
    import math
    return round(math.floor(_q(x, tick)) * tick, 2) + 0.0


def tick_up(x, tick=TICK):
    import math
    return round(math.ceil(_q(x, tick)) * tick, 2) + 0.0


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0


# ---------------------------------------------------------------- brokers
# The ten biggest Indian brokers by NSE active clients (September 2026, NSE
# broker-wise active clients via OneTradeJournal and Inc42, 2026-10-14).
# Researched 2026-10-26: docs/brokers.md has every source URL.
# "verified" lists which numbers were read on the broker's own page; anything
# else came from a secondary source or is a cautious default. Every number is
# overridable in nse/config.json: "broker": "upstox", "charges": {...},
# "squareoff": {"cas": "15:05", "other": "15:20"}.
#
# brokerage per executed order = max(min, value x pct%), then capped at cap
# (cap 0 = no cap). (0, 20, 20) is a flat Rs 20.
# squareoff = the broker's MIS auto square-off time: "cas" for F&O stocks in
# the closing auction, "other" for the rest. Valid until squareoff_until:
# SEBI moves the auction to 15:30 on 2026-12-07 and brokers will revise.
# stop_mis: how an intraday stop is held  - "sl_leg" a separate SL / SL-M
#   sell after the fill, "cover" a cover order. stop_cnc: how a delivery
#   stop+target is held - "gtt_oco", "forever_oco" (Dhan), "gtt" (single).

UNVERIFIED_SQUAREOFF = {"cas": "15:00", "other": "15:15"}   # cautious default when unknown

BROKERS = {
    "groww": {
        "name": "Groww", "rank": 1,
        "brokerage_mis": (0.1, 5.0, 20.0), "brokerage_cnc": (0.1, 5.0, 20.0), "dp_charge": 23.60,
        "squareoff": {"cas": "15:05", "other": "15:20"}, "verified": ["brokerage", "dp_charge"],
        "stop_mis": "sl_leg", "stop_cnc": "gtt_oco",
        "api": "Groww Trade API (paid monthly plan)", "mcp": "https://mcp.groww.in/mcp (official, OAuth; places orders)",
        "paper": False,
    },
    "zerodha": {
        "name": "Zerodha", "rank": 2,
        "brokerage_mis": (0.03, 0.0, 20.0), "brokerage_cnc": (0.0, 0.0, 0.0), "dp_charge": 15.34,
        "squareoff": {"cas": "15:12", "other": "15:25"}, "verified": ["brokerage", "dp_charge", "squareoff"],
        "stop_mis": "sl_leg", "stop_cnc": "gtt_oco",
        "api": "Kite Connect", "mcp": "https://mcp.kite.trade/mcp (official; hosted = read-only plus GTT; "
                                       "regular orders need a self-hosted server with your own Kite Connect key)",
        "paper": False,
    },
    "angelone": {
        "name": "Angel One", "rank": 3,
        "brokerage_mis": (0.1, 5.0, 20.0), "brokerage_cnc": (0.1, 5.0, 20.0), "dp_charge": 23.60,
        "squareoff": dict(UNVERIFIED_SQUAREOFF), "verified": ["brokerage", "dp_charge"],
        "stop_mis": "sl_leg", "stop_cnc": "gtt",
        "api": "SmartAPI (free)", "mcp": None, "paper": False,
    },
    "icicidirect": {
        "name": "ICICIdirect", "rank": 4,
        "brokerage_mis": (0.05, 0.0, 20.0), "brokerage_cnc": (0.05, 0.0, 20.0), "dp_charge": 23.60,
        "squareoff": dict(UNVERIFIED_SQUAREOFF), "verified": ["brokerage_mis"],
        "stop_mis": "sl_leg", "stop_cnc": "gtt",
        "api": "Breeze API (free; no market or intraday-margin orders through the API)", "mcp": None, "paper": False,
    },
    "upstox": {
        "name": "Upstox", "rank": 5,
        "brokerage_mis": (0.1, 0.0, 20.0), "brokerage_cnc": (0.0, 20.0, 20.0), "dp_charge": 23.60,
        "squareoff": {"cas": "15:10", "other": "15:25"}, "verified": ["brokerage", "dp_charge"],
        "stop_mis": "sl_leg", "stop_cnc": "gtt_oco",
        "api": "Upstox API", "mcp": "https://mcp.upstox.com/mcp (official, read-only)", "paper": False,
    },
    "kotak": {
        "name": "Kotak Neo", "rank": 6,
        "brokerage_mis": (0.05, 0.0, 10.0), "brokerage_cnc": (0.2, 0.0, 0.0), "dp_charge": 23.60,
        "squareoff": dict(UNVERIFIED_SQUAREOFF), "verified": ["brokerage"],
        "stop_mis": "sl_leg", "stop_cnc": "gtt",
        "api": "Neo Trade API (free)", "mcp": "https://neo.kotaksecurities.com/mcp-server/mcp (official, cannot place orders)",
        "paper": False,
    },
    "hdfcsky": {
        "name": "HDFC Sky", "rank": 7,
        "brokerage_mis": (2.5, 0.0, 20.0), "brokerage_cnc": (2.5, 0.0, 20.0), "dp_charge": 23.60,
        "squareoff": dict(UNVERIFIED_SQUAREOFF), "verified": ["brokerage"],
        "stop_mis": "sl_leg", "stop_cnc": "gtt",
        "api": "HDFC Sky Open API", "mcp": "https://api.hdfcsky.com/mcp (official; order placement not confirmed)",
        "paper": False,
    },
    "dhan": {
        "name": "Dhan", "rank": 8,
        "brokerage_mis": (0.03, 0.0, 20.0), "brokerage_cnc": (0.0, 0.0, 0.0), "dp_charge": 14.75,
        "squareoff": {"cas": "15:10", "other": "15:10"}, "verified": ["brokerage", "dp_charge", "squareoff"],
        "stop_mis": "sl_leg", "stop_cnc": "forever_oco",
        "api": "DhanHQ (trading APIs free)", "mcp": None, "paper": False,
    },
    "sbi": {
        "name": "SBI Securities", "rank": 9,
        "brokerage_mis": (0.0, 20.0, 20.0), "brokerage_cnc": (0.35, 0.0, 0.0), "dp_charge": 23.60,
        "squareoff": dict(UNVERIFIED_SQUAREOFF), "verified": [],
        "stop_mis": "sl_leg", "stop_cnc": "gtt",
        "api": None, "mcp": None, "paper": False,
    },
    "motilal": {
        "name": "Motilal Oswal", "rank": 10,
        "brokerage_mis": (0.02, 0.0, 20.0), "brokerage_cnc": (0.2, 0.0, 0.0), "dp_charge": 18.50,
        "squareoff": {"cas": "15:00", "other": "15:00"}, "verified": [],
        "stop_mis": "sl_leg", "stop_cnc": "gtt",
        "api": "Motilal Oswal API", "mcp": None, "paper": False,
    },
}
BROKER_ALIASES = {"angel": "angelone", "angel one": "angelone", "icici": "icicidirect", "kotak neo": "kotak",
                  "kotaksecurities": "kotak", "hdfc": "hdfcsky", "hdfc sky": "hdfcsky", "hdfcsec": "hdfcsky",
                  "sbisecurities": "sbi", "sbicap": "sbi", "motilaloswal": "motilal", "mosl": "motilal",
                  "kite": "zerodha"}
DEFAULT_BROKER = "zerodha"
SQUAREOFF_UNTIL = "2026-12-06"


def broker_key(cfg=None):
    name = str((cfg or {}).get("broker") or DEFAULT_BROKER).strip().lower()
    name = BROKER_ALIASES.get(name, name.replace(" ", ""))
    if name not in BROKERS:
        raise IndiaError("unknown broker %r: pick one of %s (or set its charges by hand in nse/config.json)"
                         % (name, ", ".join(sorted(BROKERS))))
    return name


def broker_profile(cfg=None):
    """The broker's profile with the user's overrides from nse/config.json."""
    k = broker_key(cfg)
    p = dict(BROKERS[k], key=k)
    so = dict(p["squareoff"])
    so.update({kk: vv for kk, vv in ((cfg or {}).get("squareoff") or {}).items() if kk in ("cas", "other")})
    p["squareoff"] = so
    p["squareoff_verified"] = "squareoff" in p["verified"] or bool((cfg or {}).get("squareoff"))
    return p


def squareoff_time(d, cas_stock, cfg=None):
    """(time, note): the broker's MIS auto square-off on day d for this stock.
    Never later than the end of continuous trading."""
    p = broker_profile(cfg)
    so = p["squareoff"]
    notes = []
    k = d.isoformat()
    in_cas = any(k >= a and (b is None or k <= b) for a, b, _ in CAS_PERIODS)
    t = parse_hhmm(so["cas"] if (cas_stock and in_cas) else so["other"], "squareoff")
    if k > SQUAREOFF_UNTIL and not (cfg or {}).get("squareoff"):
        notes.append("%s's square-off times were read before SEBI's 2026-12-07 auction change: check the "
                     "current time and set \"squareoff\" in nse/config.json" % p["name"])
    if not p["squareoff_verified"]:
        notes.append("%s's square-off time is not verified (cautious default used): check it with %s and set "
                     "\"squareoff\" in nse/config.json" % (p["name"], p["name"]))
    end = continuous_close(d, cas_stock)
    if end and t > end:
        t = end
    return t, "; ".join(notes) or None


def default_flat_by(d, cas_stock, cfg=None):
    """Be out of an MIS trade by: the user's flat_by (default 15:10), but at
    least 5 minutes before the broker's square-off."""
    want = parse_hhmm((cfg or {}).get("flat_by") or "15:10", "flat_by")
    so, note = squareoff_time(d, cas_stock, cfg)
    latest = (datetime.datetime.combine(d, so) - datetime.timedelta(minutes=5)).time()
    return min(want, latest), so, note


# ---------------------------------------------------------------- charges
# Statutory charges (the same at every broker) plus the broker's brokerage and
# DP charge from its profile above. Read 2026-10-26 (zerodha.com/charges for
# the statutory rates). Every number is overridable in nse/config.json
# "charges". MIS = intraday (squared off the same day), CNC = delivery.

CHARGES = {
    "stt_pct_mis_sell": 0.025,          # STT intraday, sell side only
    "stt_pct_cnc": 0.1,                 # STT delivery, buy and sell
    "txn_pct_nse": 0.00307,             # exchange transaction charge, both sides
    "txn_pct_bse": 0.00375,
    "sebi_per_crore": 10.0,             # SEBI turnover fee, both sides
    "stamp_pct_mis_buy": 0.003,         # stamp duty, buy side only
    "stamp_pct_cnc_buy": 0.015,
    "gst_pct": 18.0,                    # on brokerage + transaction + SEBI
}


def charges_cfg(cfg=None):
    """Statutory rates + the broker's brokerage/DP + the user's overrides."""
    c = dict(CHARGES)
    p = broker_profile(cfg)
    for m in ("mis", "cnc"):
        pct, lo, cap = p["brokerage_" + m]
        c["brokerage_pct_" + m], c["brokerage_min_" + m], c["brokerage_cap_" + m] = pct, lo, cap
    c["dp_charge_cnc_sell"] = p["dp_charge"]
    for k, v in ((cfg or {}).get("charges") or {}).items():
        if k in c:
            c[k] = float(v)
    return c


def trade_charges(buy_value, sell_value, product="MIS", exchange="NSE", cfg=None):
    """Statutory and broker charges on one round trip (or one leg: pass 0 for
    the other side). Returns a breakdown and the total, rounded to paise."""
    c = charges_cfg(cfg)
    p = str(product).upper()
    if p not in ("MIS", "CNC"):
        raise IndiaError("product is MIS (intraday) or CNC (delivery), not %r" % product)
    m = p.lower()

    def brokerage(v):
        if v <= 0:
            return 0.0
        b = max(c["brokerage_min_" + m], v * c["brokerage_pct_" + m] / 100.0)
        cap = c["brokerage_cap_" + m]
        return min(b, cap) if cap else b

    brk = brokerage(buy_value) + brokerage(sell_value)
    if p == "MIS":
        stt = sell_value * c["stt_pct_mis_sell"] / 100.0
        stamp = buy_value * c["stamp_pct_mis_buy"] / 100.0
        dp = 0.0
    else:
        stt = (buy_value + sell_value) * c["stt_pct_cnc"] / 100.0
        stamp = buy_value * c["stamp_pct_cnc_buy"] / 100.0
        dp = c["dp_charge_cnc_sell"] if sell_value > 0 else 0.0
    txn = (buy_value + sell_value) * c["txn_pct_" + ("bse" if exchange == "BSE" else "nse")] / 100.0
    sebi = (buy_value + sell_value) * c["sebi_per_crore"] / 1e7
    gst = (brk + txn + sebi) * c["gst_pct"] / 100.0
    parts = {"brokerage": brk, "stt": stt, "exchange_txn": txn, "sebi": sebi,
             "stamp_duty": stamp, "gst": gst, "dp_charge": dp}
    parts = {k: round(v, 2) + 0.0 for k, v in parts.items()}
    parts["total"] = round(sum(parts.values()), 2) + 0.0
    return parts


# ---- closing-auction stock list (same code in every skill that needs it) ----
# F&O stocks ("CAS stocks") stop continuous trading at 15:15 while the closing
# auction runs (see continuous_close). Which stocks they are: NSE's open file
# fo_mktlots.csv, cached as nse/state/fo_mktlots-YYYY-MM-DD.csv (one name for
# every skill, so one download serves all). A cache up to 7 days old is reused
# when the fetch fails, with a note. Offline: tests/fixtures/_shared/fo_mktlots.csv
# or the file given with --fo-lots.

FO_STALE_DAYS = 7
FO_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                          "tests", "fixtures", "_shared", "fo_mktlots.csv")


def _read_fo(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_fo_symbols(fh.read())


def fo_stocks(today, offline=False, fo_file=None, state_dir="nse/state", fetch=None):
    """(set of F&O stock symbols or None, note). None = could not tell."""
    if fo_file:
        if not os.path.exists(fo_file):
            raise IndiaError("no F&O list at %s (fo_mktlots.csv from %s)" % (fo_file, FO_LOTS_URL))
        return _read_fo(fo_file), None
    if offline:
        if os.path.exists(FO_FIXTURE):
            return _read_fo(FO_FIXTURE), None
        return None, ("offline and no F&O list (give one with --fo-lots PATH): treating the stock as an "
                      "F&O stock, the cautious choice (it ends continuous trading at 15:15)")
    cache = os.path.join(state_dir, "fo_mktlots-%s.csv" % today.isoformat())
    if os.path.exists(cache):
        try:
            found = _read_fo(cache)
            if found:
                return found, None
        except OSError:
            pass
    try:
        text = (fetch or (lambda u: _open_url(u, accept="text/csv")))(FO_LOTS_URL)
        found = parse_fo_symbols(text)
        if not found:
            raise IndiaError("the file had no stock rows")
        try:
            os.makedirs(state_dir, exist_ok=True)
            with open(cache, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        except OSError:
            pass
        return found, None
    except (IndiaError, OSError, ValueError) as e:
        err = str(e) or e.__class__.__name__
    best = None
    for name in (os.listdir(state_dir) if os.path.isdir(state_dir) else []):
        m = re.match(r"^fo_mktlots-(\d{4}-\d{2}-\d{2})\.csv$", name)
        if not m:
            continue
        try:
            d = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if 0 <= (today - d).days <= FO_STALE_DAYS and (best is None or d > best[0]):
            best = (d, name)
    if best:
        try:
            found = _read_fo(os.path.join(state_dir, best[1]))
            if found:
                return found, "NSE's F&O list could not be fetched (%s): using the copy cached on %s" % (
                    err, best[0].isoformat())
        except OSError:
            pass
    return None, ("NSE's F&O list could not be fetched (%s) and there is no cache from the last %d days: treating "
                  "the stock as an F&O stock, the cautious choice (it ends continuous trading at 15:15)"
                  % (err, FO_STALE_DAYS))


def cas_status(sym, day, offline=False, fo_file=None, state_dir="nse/state", fetch=None, today=None):
    """(is_cas_stock, note) for a parse_symbol() result on day. No lookup (and no
    download) when the auction rule makes no difference that day."""
    if sym.get("index") or continuous_close(day, True) == continuous_close(day, False):
        return False, None
    found, note = fo_stocks(today or to_ist(utc_now()).date(), offline, fo_file, state_dir, fetch)
    if found is None:
        return True, note
    return sym["tradingsymbol"].upper() in found, note
# ---- end closing-auction stock list ----


# ================================================================ nse-timemachine proper

TMError = IndiaError     # one error type for the whole script: main() prints it as one plain line

INTERVAL = 300       # 5-minute bars, in seconds
TRADING_BARS = 75    # 09:15 to 15:30 in 5-minute bars
# Yahoo's NSE 5-minute data carries no 15:20 and 15:25 bars (null on every day checked on
# 2026-10-26): since 2026-08-03 F&O stocks stop continuous trading at 15:15 and close through an
# auction, so a usable session is the 72 bars 09:15 to 15:15. Replays and the history therefore end
# by 15:15, not 15:30 (the forced exit is min(flat_by, continuous_close)).
SESSION_BARS = 72
OPEN_MIN = 9 * 60 + 15
CLOSE_MIN = 15 * 60 + 15      # the last time the data can reach
MARKET_CLOSE_MIN = 15 * 60 + 30
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
REF_VALUE = 100000.0     # reference position when the plan carries no quantity
DEFAULT_FLAT_BY = "15:10"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# ---------------------------------------------------------------- clock helpers

def parse_clock(text, what):
    """'10:15', '10:15 AM', '3:10' (a bare 1 to 8 o'clock means p.m.) -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants an India time like 10:15 or 15:05, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


# ---------------------------------------------------------------- data

def read_offline(path, sym):
    """A saved Yahoo chart file (.json or .json.gz), or a folder holding <SYM>-60d5m.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-60d5m.json" % sym.upper(), "%s-60d5m.json.gz" % sym.upper()):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-60d5m.json(.gz) in %s" % (sym.upper(), path))
    if not os.path.exists(path):
        raise TMError("no offline chart file %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise TMError("could not read %s: %s" % (path, e))


def fetch_chart(ysym):
    """One request for 60 days of 5-min bars (india_core's yahoo_get: query2, query1 on 429/5xx)."""
    return yahoo_get(chart_path(ysym, "60d", "5m"))


def chart_result(text, sym):
    try:
        data = json.loads(text)
    except ValueError:
        raise TMError("Yahoo returned something that is not JSON for %s" % sym.upper())
    chart = data.get("chart") if isinstance(data, dict) else None
    if not isinstance(chart, dict):
        raise TMError("Yahoo returned something unexpected for %s" % sym.upper())
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise TMError("Yahoo has no data for %s: %s" % (sym.upper(), err.get("description") or "unknown ticker"))
    return res[0]


def parse_bars(result, now_utc):
    """Yahoo chart result -> (meta, bars, dropped). Regular-session 5-min bars only,
    oldest first. Dropped: bars with a missing price, bars off the 5-minute grid
    (the live tick, e.g. 10:28:14), bars not complete yet (now < start + 5 min,
    where now is the earlier of --now and the data's own regularMarketTime), and
    bars outside 09:15 to 15:30 IST (pre-open, closing auction)."""
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    as_of = now_utc
    rmt = meta.get("regularMarketTime")
    if isinstance(rmt, (int, float)) and utc_of(rmt) < as_of:
        as_of = utc_of(rmt)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for i, ts in enumerate(stamps):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c):
            dropped["no_price"] += 1
            continue
        ts = int(ts)
        if ts % INTERVAL:
            dropped["off_grid"] += 1
            continue
        start = utc_of(ts)
        local = to_ist(start)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= TRADING_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if start + datetime.timedelta(seconds=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        bars[ts] = {"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                    "o": o, "h": h, "l": l, "c": c, "v": v or 0}
    return meta, [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def full_sessions(days, before):
    """Full sessions (all 72 usable bars, 09:15 to 15:15) strictly before the plan's date, oldest first,
    and the short ones that were left out (special sessions, gaps)."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if all(i in s for i in range(SESSION_BARS)):
            full.append((d, [s[i] for i in range(SESSION_BARS)]))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short


# ---------------------------------------------------------------- R with charges

class Econ(object):
    """What a trade is worth in rupees and in R. 1R = the max loss at the stop,
    charges included. Prices are per share; qty is whole shares."""

    def __init__(self, entry, stop, target, qty, side="buy", product="MIS", exchange="NSE", cfg=None):
        self.entry, self.stop, self.target, self.qty = entry, stop, target, int(qty)
        self.long = side == "buy"
        self.product, self.exchange, self.cfg = product, exchange, cfg
        self.charges_at_stop = self.fees(stop)
        self.charges_at_target = self.fees(target)
        self.risk_gross = abs(entry - stop) * self.qty
        self.risk_cash = self.risk_gross + self.charges_at_stop
        self.net_at_target = self.net(target)
        self.r_target = round(self.net_at_target / self.risk_cash, 4)
        self.gross_r = round(abs(target - entry) / abs(entry - stop), 4)

    def fees(self, exit_price):
        e, x = self.entry * self.qty, exit_price * self.qty
        buy, sell = (e, x) if self.long else (x, e)
        return trade_charges(buy, sell, self.product, self.exchange, self.cfg)["total"]

    def net(self, exit_price):
        gross = ((exit_price - self.entry) if self.long else (self.entry - exit_price)) * self.qty
        return gross - self.fees(exit_price)

    def r_at(self, exit_price):
        return self.net(exit_price) / self.risk_cash


# ---------------------------------------------------------------- the simulation

def build_pool(sessions):
    """pool[i] = every past session's bar i as (o, h, l, c) / bar i-1's close, in date order."""
    pool = [[] for _ in range(SESSION_BARS)]
    for _, D in sessions:
        for i in range(1, SESSION_BARS):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, econ, k, last, replays=10000, seed=19, window=2):
    """Monte Carlo replays of bars k+1..last. Same draws and order as the US Time Machine."""
    long = econ.long
    entry, stop, target = econ.entry, econ.stop, econ.target

    def slot(rng, i):
        return i if window == 0 else min(SESSION_BARS - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    Rs = [econ.r_target if o == "target" else -1.0 if o == "stop" else econ.r_at(p)
          for o, p in zip(outcomes, closes)]
    green = sum(1 for r in Rs if r > 0)
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "r_at_target": econ.r_target, "gross_r": econ.gross_r,
        "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


# ---------------------------------------------------------------- stop rules and the history replay

def tick_away(x, long):
    """Round a stop to the 0.05 tick, away from the entry: down for a buy, up for a sell."""
    return tick_down(x) if long else tick_up(x)


def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list."""
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry, long):
    r = rule["stop_rule"]
    if r == "or_mid":
        return tick_away((oh + ol) / 2, long)
    if r in ("or_low", "or_high"):
        return tick_away(ol if long else oh, long)
    frac = rule["stop_pct"] / 100.0
    return tick_away(entry * (1 - frac) if long else entry * (1 + frac), long)


def history_replay(sessions, rule, last, side="buy", cost=None):
    """The plan's rule on every past full session: entry = the first bar after the
    opening range to close beyond it (up to the exit bar), then stop first, then
    target, bar by bar, else the close at the exit time. R includes charges (cost =
    {"qty", "product", "exchange", "cfg"})."""
    long = side == "buy"
    cost = cost or {"qty": 1, "product": "MIS", "exchange": "NSE", "cfg": None}
    n = rule["or_minutes"] // 5
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = tick_up(D[kk]["c"]) if long else tick_down(D[kk]["c"])
        s = rule_stop(rule, oh, ol, e, long)
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        raw_t = e + rule["target_r"] * risk if long else e - rule["target_r"] * risk
        t = tick_down(raw_t) if long else tick_up(raw_t)
        if (t <= e) if long else (t >= e):
            continue
        ec = Econ(e, s, t, cost["qty"], side, cost["product"], cost["exchange"], cost["cfg"])
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        R = ec.r_target if oc == "target" else -1.0 if oc == "stop" else ec.r_at(cl)
        trades.append({"date": d, "time": clock24(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def _read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise TMError("%s is not valid JSON: %s" % (path, e))


def load_plan(a, now_utc, cfg=None):
    """The plan from --plan (nse/plan.json) or from the flags, checked and normalized."""
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or to_ist(now_utc).date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": "5m", "product": a.product}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: run /nse-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % a.plan)
        raw = _read_json(a.plan)
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        src = a.plan
    if isinstance(raw, dict) and raw.get("cas_stock") is None:
        # an older plan or flags: look the stock up in NSE's F&O list (unknown = F&O, the cautious choice)
        try:
            sym = parse_symbol(raw.get("symbol") or raw.get("yahoo") or "", str(raw.get("exchange") or "NSE").upper())
            day = datetime.date.fromisoformat(str(raw.get("date") or ""))
            raw = dict(raw)
            raw["cas_stock"], raw["fo_note"] = cas_status(sym, day, offline=bool(a.offline), fo_file=a.fo_lots)
        except ValueError:
            pass      # normalize_plan says what is wrong with the date
        except IndiaError as e:
            if "F&O list" in str(e):
                raise
    return normalize_plan(raw, src, cfg)


def normalize_plan(raw, src="flags", cfg=None):
    """A plan dict (nse/plan.json shape) -> the checked plan the time machine runs.
    New fields (product, exchange, yahoo, flat_by) fall back to MIS, NSE, the config's flat_by or 15:10."""
    text = raw.get("symbol") or raw.get("yahoo") or ""
    try:
        ps = parse_symbol(text, str(raw.get("exchange") or "NSE").upper())
    except IndiaError:
        raise TMError("the plan needs an NSE/BSE ticker symbol, got %r" % raw.get("symbol"))
    if ps["index"]:
        raise TMError("%s is an index: it cannot be traded, plan a stock" % ps["tradingsymbol"])
    if raw.get("yahoo") and str(raw["yahoo"]).upper() != ps["yahoo"]:
        ps = parse_symbol(raw["yahoo"])
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    product = str(raw.get("product") or "MIS").upper()
    if product not in ("MIS", "CNC"):
        raise TMError("product is MIS (intraday) or CNC (delivery), got %r" % raw.get("product"))
    if side == "sell" and product == "CNC":
        raise TMError("a CNC (delivery) trade cannot be a short sale: sell needs product MIS")
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): Yahoo keeps 60 days of 5-min bars but only about "
                      "7 days of 1-min bars, too few sessions for a fair replay pool" % interval)
    try:
        d = datetime.date.fromisoformat(str(raw.get("date") or ""))
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-26, got %r" % raw.get("date"))
    date = d.isoformat()
    if date in SPECIAL_SESSIONS:
        raise TMError("%s is a special session (%s): its hours are not the regular 09:15 to 15:30 grid, so the "
                      "replay pool does not fit it" % (date, SPECIAL_SESSIONS[date]["name"]))
    ok, why = trading_day(d)
    if not ok:
        raise TMError("%s is not an NSE trading day (%s)" % (date, why))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 10:15)"
                          % raw["entry_time"])
        k = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 10:15) or entry_bar")
    if not 0 <= k <= SESSION_BARS - 2:
        raise TMError("the entry must close between 09:20 and 15:10 to leave bars to replay (got %s)"
                      % clock24(OPEN_MIN + 5 * (k + 1)))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= 375:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    cas = raw.get("cas_stock")
    if cas is not None and not isinstance(cas, bool):
        raise TMError("cas_stock in the plan is true or false, not %r" % cas)
    flat = raw.get("flat_by") or (cfg or {}).get("flat_by") or DEFAULT_FLAT_BY
    parse_clock(flat, "flat_by")
    qty = raw.get("quantity", raw.get("qty"))
    if qty is not None:
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise TMError("the plan's quantity must be a whole number of shares, got %r" % qty)
        if qty < 1:
            raise TMError("the plan's quantity must be at least 1 share")
    qty_source = "plan" if qty else None
    if qty is None:      # nse-exit writes charges.qty: the reference position its charges line was computed on
        cq = (raw.get("charges") or {}).get("qty") if isinstance(raw.get("charges"), dict) else None
        if isinstance(cq, int) and not isinstance(cq, bool) and cq >= 1:
            qty, qty_source = cq, "plan_charges"
    risk = (entry - stop) if long else (stop - entry)
    return {
        "symbol": ps["tradingsymbol"], "yahoo": ps["yahoo"], "exchange": ps["exchange"], "product": product,
        "side": side, "date": date, "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock24(OPEN_MIN + 5 * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto").replace("-", "_"), "interval": "5m",
        "flat_by": clock24(parse_clock(flat, "flat_by")), "quantity": qty, "qty_source": qty_source,
        "cas_stock": bool(cas), "fo_note": raw.get("fo_note"), "from": src,
    }


def resolve_rule(plan, entry_day):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(tick_away((oh + ol) / 2, long) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_mid"
            elif abs(tick_away(ol if long else oh, long) - plan["stop"]) < 0.005:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the run

def _until(mc):
    return mc.get("until") or clock24(CLOSE_MIN)


def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's net R. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            _until(mc), _until(mc))
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    g = mc.get("gross_r") or r
    one_r = abs(g - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if abs(r - 1.0) < 0.005:
        return "%s: %s." % (head, odds)
    if abs(g - r) < 0.005:
        lead = "at %gR the target" % round(r, 2)
    else:
        lead = "after charges the %gR target is worth %.2fR, so it" % (round(g, 2), r)
    return ("%s: %s has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, lead, 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, _until(mc), _until(mc)))
    lv = lean(mc)
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    return notes


def time_machine(text, plan, now_utc, replays=10000, seed=19, window=2, until=None, source="yahoo",
                 cfg=None, qty=None):
    """Chart JSON text + a normalized plan -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    sym = plan["symbol"]
    result = chart_result(text, sym)
    meta, bars, dropped = parse_bars(result, now_utc)
    msym = str(meta.get("symbol") or "").upper()
    if msym and msym != plan["yahoo"].upper():
        raise TMError("the data is for %s but the plan is for %s" % (msym, plan["yahoo"]))
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % gran)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % sym)
    k = plan["entry_bar"]
    product = plan["product"]
    warn = []
    flat_min = parse_clock(plan["flat_by"], "flat_by")
    pday = datetime.date.fromisoformat(plan["date"])
    cas = bool(plan.get("cas_stock"))
    cc = continuous_close(pday, cas)
    end_min = min(CLOSE_MIN, cc.hour * 60 + cc.minute)       # the forced exit is min(flat_by, continuous_close)
    if plan.get("fo_note"):
        warn.append(plan["fo_note"])
    if cas and cc < session(pday)[1]:
        warn.append("Closing auction: %s is an F&O stock, so continuous trading ends at %s, not %s, and the auction "
                    "takes no stop-loss orders; every replay is out by then" % (sym, hm(cc), hm(session(pday)[1])))
    if until is None:
        if product == "MIS":
            until_min = min(end_min, OPEN_MIN + ((flat_min - OPEN_MIN) // 5) * 5)
        else:
            until_min = end_min
    else:
        until_min = parse_clock(until, "--until")
        if (until_min - OPEN_MIN) % 5 or until_min > end_min:
            raise TMError("--until must be on the 5-minute grid and no later than %s (%s)" % (
                clock24(end_min), "the end of continuous trading for this stock" if end_min < CLOSE_MIN or cas
                else "the last bar Yahoo carries for NSE stocks"))
        if product == "MIS" and until_min > flat_min:
            warn.append("--until %s is later than the plan's flat_by %s: a MIS trade should be out by then"
                        % (clock24(until_min), plan["flat_by"]))
    squareoff = None
    if product == "MIS":
        so, so_note = squareoff_time(pday, cas, cfg)
        squareoff = clock24(so.hour * 60 + so.minute)
        if so_note:
            warn.append(so_note)
        if flat_min >= so.hour * 60 + so.minute:
            warn.append("flat_by %s is at or after %s's automatic MIS square-off (%s, with a fee): plan an earlier "
                        "exit" % (plan["flat_by"], broker_profile(cfg)["name"], squareoff))
        elif flat_min >= end_min and until is None:
            warn.append("flat_by %s is at or after the end of continuous trading (%s): the replay exits at %s"
                        % (plan["flat_by"], clock24(end_min), clock24(end_min)))
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise TMError("%s leaves nothing to replay after the %s entry" % (
            ("the exit time %s (flat_by)" % clock24(until_min)) if until is None else "--until %s" % clock24(until_min),
            plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        reason = ("no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock24(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    if not eb["l"] - 0.05 <= plan["entry"] <= eb["h"] + 0.05:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                    % (plan["entry"], eb["l"], eb["h"]))
    note = calendar_note(datetime.date.fromisoformat(plan["date"]))
    if note:
        warn.append(note.rstrip("."))
    sessions, short = full_sessions(days, plan["date"])
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), sym, plan["date"], MIN_SESSIONS))
    # the money: a plan quantity, else a reference position of about Rs 1,00,000
    q = qty or plan.get("quantity")
    basis = ("given" if qty else plan.get("qty_source")) if q else "reference"
    q = int(q) if q else max(1, int(REF_VALUE // plan["entry"]))
    side = plan["side"]
    econ = Econ(plan["entry"], plan["stop"], plan["target"], q, side, product, plan["exchange"], cfg)
    pool = build_pool(sessions)
    mc = simulate(pool, econ, k, last, replays, seed, window)
    mc = dict(mc, until=clock24(until_min), exit_rule="flat_by" if (product == "MIS" and until is None) else
              ("close" if until_min == end_min else "until"),
              squareoff=squareoff, pool_sessions=len(sessions), pool_from=sessions[0][0], pool_to=sessions[-1][0],
              band_times=[clock24(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    costs = {"qty": q, "qty_basis": basis, "product": product, "exchange": plan["exchange"],
             "risk_gross": round(econ.risk_gross, 2), "charges_at_stop": round(econ.charges_at_stop, 2),
             "one_R": round(econ.risk_cash, 2), "charges_at_target": round(econ.charges_at_target, 2),
             "net_at_target": round(econ.net_at_target, 2), "r_at_target": econ.r_target,
             "gross_r": econ.gross_r}
    rule = resolve_rule(plan, entry_day)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, side,
                                 {"qty": q, "product": product, "exchange": plan["exchange"], "cfg": cfg})
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    out = {
        "created_by": "nse-timemachine",
        "created_at": iso(now_utc),
        "source": source,
        "data": {"symbol": msym or plan["yahoo"], "interval": "5m", "range": "60d",
                 "last_bar": "%s %s IST" % (bars[-1]["date"], bars[-1]["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "costs": costs,
        "mc": mc,
        "history": history,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d full sessions. It is not a prediction: results, "
                 "news, circuits and halts can do things those sessions never did. Same seed, same numbers."
                 % (sym, len(sessions))),
    }
    if product == "CNC":
        out["product_note"] = ("CNC (delivery): the intraday replay does not model overnight holding. It only "
                               "simulates today until %s (the last bar Yahoo carries: F&O stocks end continuous trading at "
                               "15:15 since 2026-08-03, and the market closes at %s); gaps, news "
                               "and price moves on later days, and T+1 settlement are not in these numbers."
                               % (clock24(end_min), hm(CLOSE)))
    return out


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h, c = doc["plan"], doc["mc"], doc["history"], doc["costs"]
    long = p["side"] == "buy"
    until = m["until"]
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    why = {"flat_by": "the plan's flat-by exit", "close": "the end of the data"}.get(m.get("exit_rule"), "your --until time")
    lines = [
        "%s %s (%s, %s), %s %s: entry %s at %s (bar %d), stop %s, target %s (%sR, risk %s a share)."
        % (p["symbol"], p["side"], p["exchange"], p["product"], wd, p["date"], inr(p["entry"]), p["entry_time"],
           p["entry_bar"], inr(p["stop"]), inr(p["target"]), ("%g" % r), inr(p["risk_per_share"])),
        "%s replays of the rest of the session, %s to %s (%d five-minute bars, %s), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], why, m["seed"]),
        "Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
        "%d full sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"], m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s%s" % (pc(m["p_neither"]), until,
                                                      " (squared off there)" if p["product"] == "MIS" else ""),
        "  green          %-8s finished in profit after charges (target first, or net gain at %s)"
        % (pc(m["p_green"]), until),
        "",
        "  Price at %s: median %s, 90 %% of replays between %s and %s."
        % (until, inr(m["median_close"]), inr(m["p5_close"]), inr(m["p95_close"])),
        "  Average R: %+.3f (net of charges)." % m["avg_R"],
        "",
        "R and charges (%d shares, %s): 1R = %s = %s risk + %s charges if the stop is hit. The target nets %s "
        "after %s charges = %+.2fR%s."
        % (c["qty"], {"plan": "your plan's quantity", "given": "the quantity you gave",
            "plan_charges": "reference position from the plan"}.get(c["qty_basis"]) or
           "a reference position of about %s; nse-budget sizes the real one" % inr(REF_VALUE, 0),
           inr(c["one_R"]), inr(c["risk_gross"]), inr(c["charges_at_stop"]), inr(c["net_at_target"]),
           inr(c["charges_at_target"]), c["r_at_target"],
           "" if abs(c["gross_r"] - c["r_at_target"]) < 0.005 else " (a %gR target before charges)" % c["gross_r"]),
        "",
    ]
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high"}.get(ru["stop_rule"],
                                                           "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f."
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"]))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    if doc.get("product_note"):
        lines += [doc["product_note"], ""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (special sessions or gaps): %s."
                     % (len(doc["data"]["short_sessions_left_out"]),
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")


def compute(a):
    now = parse_now(a.now)
    load_calendar()
    cfg = _read_json(a.config, {}) or {}
    plan = load_plan(a, now, cfg)
    if a.offline:
        text, source = read_offline(a.offline, plan["symbol"]), "offline"
    else:
        text, source = fetch_chart(plan["yahoo"]), "yahoo"
    return time_machine(text, plan, now, a.replays, a.seed, a.window, a.until, source, cfg, a.qty)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2, ensure_ascii=False))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /nse-budget sizes it to your budget and your max loss per trade.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned NSE/BSE trade (nse-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="nse/plan.json", help="the plan from nse-exit")
        p.add_argument("--config", default="nse/config.json", help="charges overrides and flat_by")
        p.add_argument("--symbol", help="or give the plan as flags: ticker (RELIANCE, TCS.NS, RELIANCE.BO)")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--product", default="MIS", choices=["MIS", "CNC"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, India)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help='the entry bar\'s close, e.g. "10:15"')
        p.add_argument("--qty", type=int, help="shares, for the charges (default: the plan's, else about Rs 1,00,000)")
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this time (default: the plan's flat_by for MIS, 15:30 for CNC)")
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart JSON (.json or .json.gz), no network")
        p.add_argument("--fo-lots", metavar="PATH", help="fo_mktlots.csv (which stocks end at 15:15 in the closing "
                       "auction); default: NSE's file cached in nse/state, or the test fixture with --offline")
        p.add_argument("--now", help='pretend it is this time ("2026-10-26 10:30" India time, or ISO with Z)')

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="nse/timemachine.json", metavar="PATH",
                   help="save the result (default nse/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="nse/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except TMError as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
