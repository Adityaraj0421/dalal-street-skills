#!/usr/bin/env python3
"""Price alerts and the NSE clock for nse-alerts. Standard library only. Python 3.8+.

    python3 alerts.py clock                          # is the NSE open? next open / close
    python3 alerts.py quote RELIANCE --write         # quotes -> nse/prices.json
    python3 alerts.py quote --holdings --write       # every ticker in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target, flat-by and circuit from nse/plan.json
    python3 alerts.py add --symbol RELIANCE --stop 1183.65
    python3 alerts.py add --symbol RELIANCE --target 1203.55
    python3 alerts.py add --at 15:10 --label "flat by" --before-close
    python3 alerts.py add --symbol TCS --pct 3
    python3 alerts.py add --symbol JPPOWER --circuit --within 1
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-26 15:10" --offline tests/fixtures/nse-alerts
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  circuit         the price is within "within" percent of the stock's upper or
                  lower circuit (default min(1, band / 4)); "side": "upper" or
                  "lower" for one side. The band comes from NSE's sec_list.csv,
                  the limit is previous close x (1 +- band %), on the 0.05 tick.
                  Stocks with "No Band" (most F&O stocks) have dynamic bands:
                  the alert says so once a day and cannot compute a limit.
  opening_bell    the NSE opened today (rings once per trading day)
  time            the India clock passed "at" (HH:MM IST) on a trading day
                  (rings once per trading day); "before_close": true moves it
                  to 5 minutes before the close on a special session (Muhurat
                  trading), so a 15:10 "flat by" rings 5 minutes before that
                  day's close instead

A rule with "date" is only watched on that India date (rules from a plan are
for the plan's day only). An above / below alert rings ONCE and then stays
quiet until you reset it, unless the rule has "rearm": true (it re-arms when
the price crosses back). A pct_move or circuit alert rings at most once a day.

Quotes (free, delayed, unofficial; for alerts only, never for timing an order):
  Yahoo  https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m
         (.NS for NSE, .BO for BSE e.g. RELIANCE.BO, indices NIFTY / BANKNIFTY /
         SENSEX; falls back to query1 on a 429 or a 5xx; one retry with a
         pause on a 429; one request per ticker, half a second apart).
         Stooq has no Indian tickers, so Yahoo is the only source.
  --offline DIR reads DIR/yahoo-<KEY>.json (or .json.gz) and DIR/sec_list.csv.
  Band: https://archives.nseindia.com/content/equities/sec_list.csv (plain CSV,
         cached once a day in nse/state/sec_list.json).

Price keys: an NSE stock is keyed by its symbol (RELIANCE), a BSE stock by its
Yahoo symbol (RELIANCE.BO), an index by its name (NIFTY). nse/prices.json is
keyed the same way and says which Yahoo symbol each came from.

Calendar: NSE holidays, weekends and special sessions (Muhurat trading) come
from the shared india_core calendar; add next year's holidays and the Muhurat
timings to nse/holidays.json. Outside the years it knows, the clock says so.
"""

import argparse
import csv
import gzip
import io

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; dalal-street-skills/1.0)"
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
        "squareoff": {"cas": "15:05", "other": "15:05"}, "verified": ["brokerage", "squareoff"],
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


# ================================================================ nse-alerts proper

AlertError = IndiaError      # one error type for the whole script: main() prints it as one plain line

PAUSE = 0.5            # seconds between requests: be polite to free endpoints
SEC_LIST_URL = "https://archives.nseindia.com/content/equities/sec_list.csv"
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DEFAULT_FLAT_BY = "15:10"
SPECIAL_FLAT_BEFORE_CLOSE = 5     # minutes: on a special session flat-by = close - 5 minutes
# Diwali Muhurat trading is announced by NSE a few weeks ahead. Until its timings are in
# nse/holidays.json the clock says so instead of calling the day a plain closed Sunday.
MUHURAT_EXPECTED = {"2026-11-08": "Diwali Muhurat trading"}
_sleep = time.sleep    # tests swap this and _open_url


# ---------------------------------------------------------------- the NSE clock

def market_status(now_utc, cas_stock=None):
    """The NSE clock. cas_stock: None = no stock asked about, True/False = an F&O stock (whose continuous
    trading ends early in the closing auction) or not."""
    local = to_ist(now_utc)
    d = local.date()
    k = d.isoformat()
    ok, reason = trading_day(d)
    special = SPECIAL_SESSIONS.get(k)
    st = {"now_ist": local.strftime("%Y-%m-%d %H:%M"), "date": k, "weekday": local.strftime("%A"),
          "calendar_known": d.year in CALENDAR_YEARS, "open": False, "reason": reason,
          "special": special["name"] if special else None}
    s = session(d)
    if s:
        o, c = s
        st["opens"], st["closes"] = hm(o), hm(c)
        fo_end = continuous_close(d, True)
        if fo_end < c:
            st["fo_close"] = hm(fo_end)           # F&O stocks stop continuous trading here (closing auction)
        if cas_stock is not None:
            st["cas_stock"] = bool(cas_stock)
            st["continuous_close"] = hm(continuous_close(d, cas_stock))
        if o <= local.time() < c:
            st["open"] = True
            st["reason"] = "special session" if special else "regular session"
        elif local.time() < o:
            st["reason"] = ("pre-open: the market opens at %s" % hm(o)
                            if datetime.time(9, 0) <= local.time() and not special else
                            ("before the open: the special session starts at %s" % hm(o) if special
                             else "before the open"))
        else:
            st["reason"] = "after the close"
    if s and local.time() < s[0]:
        st["next_open"] = "%s %s" % (k, hm(s[0]))
    else:
        st["next_open"] = next_open(d)
    for mk, name in sorted(MUHURAT_EXPECTED.items()):
        days = (datetime.date.fromisoformat(mk) - d).days
        if mk not in SPECIAL_SESSIONS and 0 <= days <= 45:
            st["muhurat_note"] = ("%s is expected on %s but NSE's timings are not in the calendar yet: once NSE "
                                  "announces them, add them to nse/holidays.json under special_sessions." % (name, mk))
    for mk, sp in sorted(SPECIAL_SESSIONS.items()):
        days = (datetime.date.fromisoformat(mk) - d).days
        if 0 < days <= 45:
            st["muhurat_note"] = "%s on %s: %s to %s IST (F&O stocks stop continuous trading at %s)." % (
                sp["name"], mk, hm(sp["open"]), hm(sp["close"]), hm(continuous_close(datetime.date.fromisoformat(mk), True)))
    return st


# ---------------------------------------------------------------- symbols and quotes

def skey(text, default_exchange="NSE"):
    """The key a ticker goes by in rules and in prices.json: RELIANCE (NSE), RELIANCE.BO (BSE), NIFTY (index)."""
    ps = parse_symbol(text, default_exchange)
    return ps["yahoo"] if ps["exchange"] == "BSE" else ps["tradingsymbol"]


def parse_yahoo(text, key):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % key)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (key, err.get("description", "unknown ticker")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % key)
    t = m.get("regularMarketTime")
    when = iso(utc_of(t)) if t else ""
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": key, "price": float(price), "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("previousClose") or m.get("chartPreviousClose"), "time": when,
            "currency": m.get("currency"), "yahoo": m.get("symbol"), "source": "yahoo"}


def fetch_yahoo(key):
    """One request: today's 5-minute chart (its meta carries the price and the previous close)."""
    return yahoo_get(chart_path(parse_symbol(key)["yahoo"], "1d", "5m"))


def _read_text(base):
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            return fh.read()
    if os.path.exists(base + ".gz"):
        with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    raise AlertError("no offline file %s" % base)


def get_quotes(keys, offline=None, pause=PAUSE):
    """{KEY: quote} and [errors]. One request per ticker, with a pause between."""
    quotes, errors = {}, []
    for i, key in enumerate(keys):
        try:
            key = skey(key)
            if offline:
                text = _read_text(os.path.join(offline, "yahoo-%s.json" % key))
            else:
                if i and pause:
                    _sleep(pause)
                text = fetch_yahoo(key)
            quotes[key] = parse_yahoo(text, key)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc, source="yahoo"):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = iso(now_utc)
    for s, q in quotes.items():
        ps = parse_symbol(s)
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "yahoo": q.get("yahoo") or ps["yahoo"], "exchange": ps["exchange"], "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
           "note": ("Delayed quotes in rupees for alerts and sizing checks. Your broker's price is the one that "
                    "fills. detail.fetched is when each price was read."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- price bands (circuit limits)

def parse_sec_list(text):
    """sec_list.csv -> {SYMBOL: {"band": 20.0 | None, "series": "EQ", "remarks": ""}}.
    Columns carry stray spaces. "No Band" (dynamic bands) is None. A symbol with several
    series keeps its EQ row, else the first."""
    rows = csv.reader(io.StringIO(text.lstrip("﻿")))
    head = next(rows, None)
    if not head or [h.strip().lower() for h in head[:4]] != ["symbol", "series", "security name", "band"]:
        raise AlertError("sec_list.csv did not look like NSE's price band list (Symbol,Series,Security Name,Band,Remarks)")
    bands = {}
    for r in rows:
        if len(r) < 4:
            continue
        sym, series, band = r[0].strip().upper(), r[1].strip().upper(), r[3].strip()
        remarks = r[4].strip() if len(r) > 4 else ""
        try:
            b = float(band)
        except ValueError:
            b = None
        entry = {"band": b, "series": series, "remarks": "" if remarks in ("-", "") else remarks}
        if sym and (sym not in bands or (series == "EQ" and bands[sym]["series"] != "EQ")):
            bands[sym] = entry
    if not bands:
        raise AlertError("sec_list.csv had no rows")
    return bands


def get_bands(cache, now_utc, offline=None):
    """(bands, warning). Fetched at most once per India date and cached in `cache`."""
    today = to_ist(now_utc).date().isoformat()
    if offline:
        return parse_sec_list(_read_text(os.path.join(offline, "sec_list.csv"))), None
    old = _read(cache, None)
    if isinstance(old, dict) and old.get("fetched") == today and old.get("bands"):
        return old["bands"], None
    try:
        text = _open_url(SEC_LIST_URL, accept="text/csv")
        bands = parse_sec_list(text)
    except (urllib.error.URLError, OSError, AlertError) as e:
        if isinstance(old, dict) and old.get("bands"):
            return old["bands"], ("could not refresh NSE's price band list (%s): using the copy from %s"
                                  % (e, old.get("fetched")))
        raise AlertError("could not read NSE's price band list %s: %s" % (SEC_LIST_URL, e))
    _write(cache, {"fetched": today, "source": SEC_LIST_URL, "bands": bands})
    return bands, None


def circuit_limits(prev_close, band):
    """(lower, upper) on the 0.05 tick, kept inside the exact band: previous close x (1 +- band %).
    NSE's own page shows the exact limit; this is for a heads-up."""
    return (tick_up(prev_close * (1 - band / 100.0)), tick_down(prev_close * (1 + band / 100.0)))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "circuit", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "circuit":
        if rule.get("within") is not None and not (isinstance(rule["within"], (int, float)) and rule["within"] >= 0):
            raise AlertError("rule %s: within is a percent distance like 1" % rule.get("id"))
        if rule.get("side") not in (None, "upper", "lower"):
            raise AlertError("rule %s: side is upper or lower" % rule.get("id"))
        ps = parse_symbol(rule["symbol"])
        if ps["exchange"] != "NSE":
            raise AlertError("rule %s: circuit alerts use NSE's price band list, so NSE stocks only (not %s)"
                             % (rule.get("id"), rule["symbol"]))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an India time like \"15:10\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that India date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this India date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return to_ist(utc).date().isoformat() == today


def ring_time(rule, status):
    """The time a time rule rings today. With "before_close" it moves to 5 minutes before
    the close on a special session (Muhurat trading is about an hour long)."""
    at = _hhmm(rule.get("at"))
    if rule.get("before_close") and status.get("special") and status.get("closes"):
        # an F&O stock's continuous trading ends earlier (closing auction)
        close = _hhmm(status["fo_close"] if rule.get("cas_stock") and status.get("fo_close") else status["closes"])
        at = (datetime.datetime.combine(datetime.date(2000, 1, 1), close)
              - datetime.timedelta(minutes=SPECIAL_FLAT_BEFORE_CLOSE)).time()
    return at


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def _skey(sym):
    try:
        return skey(sym)
    except AlertError:
        return str(sym).upper()


def evaluate(rules, quotes, status, state, now_utc, bands=None):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}.
    quotes: {KEY: quote}; bands: {SYMBOL: {"band", "series", "remarks"}} for circuit rules."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    local = to_ist(now_utc)
    stamp = iso(now_utc)
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                what = ("%s session" % status["special"]) if status.get("special") else "regular session"
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: the NSE is open (%s IST, %s %s to %s)."
                              % (status["now_ist"], what, status["opens"], status["closes"])})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = ring_time(r, status)
            if status.get("opens") and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:],
                                                           local.strftime("%H:%M"), at.strftime("%H:%M"))
                msg += (", the market closes at %s." % status["closes"]) if status["open"] else \
                    ", the market has closed."
                sym = _skey(r["symbol"]) if r.get("symbol") else ""
                if sym:
                    msg += " If %s is still open, close it" % sym
                    if r.get("product") == "MIS":
                        msg += (": an open MIS position is squared off by the broker from about 15:20, with a "
                                "fee. nse-broker can build the sell ticket, then nse-risk and your yes.")
                    else:
                        msg += "."
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = _skey(r["symbol"])
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s: %s your %s %s%s."
                              % (sym, "is" if status["open"] else "last", inr(p), word, label, inr(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s%s)."
                              % (sym, move, inr(p), inr(float(ref)),
                                 "" if r.get("ref") else " previous close")})
                rung[rid] = stamp
        elif typ == "circuit":
            b = (bands or {}).get(sym)
            if not b:
                continue
            if b.get("band") is None:
                if not _rang_today(rung.get(rid), today):
                    rings.append({"id": rid, "symbol": sym, "message":
                                  "%s has no fixed price band (\"No Band\": dynamic bands apply), so a circuit "
                                  "distance cannot be worked out here. Check its live limits on NSE's website."
                                  % sym})
                    rung[rid] = stamp
                continue
            prev = q.get("prev_close")
            if not prev:
                continue
            band = float(b["band"])
            lower, upper = circuit_limits(float(prev), band)
            within = r["within"] if r.get("within") is not None else min(1.0, band / 4.0)
            for side, limit in (("upper", upper), ("lower", lower)):
                if r.get("side") and r["side"] != side:
                    continue
                gap = (limit - p) / p * 100.0 if side == "upper" else (p - limit) / p * 100.0
                key = "%s:%s" % (rid, side)
                if gap <= within and not _rang_today(rung.get(key), today):
                    if gap <= 0:
                        where = "at its %s circuit %s" % (side, inr(limit))
                    else:
                        where = "%.2f %% from its %s circuit %s" % (gap, side, inr(limit))
                    rings.append({"id": key, "symbol": sym, "message":
                                  "%s is %s, %s (%g %% band on the previous close %s). Orders can sit unfilled at a "
                                  "circuit%s." % (sym, inr(p), where, band, inr(float(prev)),
                                                  ": at the lower circuit there may be no buyers, so a stop may "
                                                  "not get you out" if side == "lower" else
                                                  ": at the upper circuit you may not be able to buy more")})
                    rung[key] = stamp
    state["last_check"] = stamp
    return rings, state


def no_circuit_reason(key, bands):
    """Why a circuit alert cannot be set for this NSE stock, or None when it can."""
    b = (bands or {}).get(key.upper())
    if b is None:
        return "%s is not in NSE's price band list, so no circuit alert was set" % key
    if b.get("band") is None:
        return ("%s has no fixed price band (\"No Band\": F&O stocks have dynamic bands), so no circuit alert was set"
                % key)
    return None


def rules_from_plan(plan, cfg_flat_by=None, bands=None, cfg=None):
    """nse/plan.json -> a stop alert, a target alert, a circuit alert (NSE stocks with a fixed band in
    `bands`) and, for MIS, a flat-by time alert, all for the plan's day. A CNC plan is held overnight,
    so no flat-by. Without `bands` there is no circuit alert. A plan without flat_by gets
    default_flat_by() for the stock and the configured broker (cfg)."""
    if not isinstance(plan, dict) or not (plan.get("yahoo") or plan.get("symbol")):
        raise AlertError("the plan has no symbol (run nse-exit first)")
    ps = parse_symbol(plan.get("yahoo") or plan["symbol"], str(plan.get("exchange") or "NSE").upper())
    key = ps["yahoo"] if ps["exchange"] == "BSE" else ps["tradingsymbol"]
    product = str(plan.get("product") or "MIS").upper()
    out = []
    for field, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(field)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run nse-exit)" % field)
        label = plan.get("%s_label" % field)
        out.append({"id": "%s-plan-%s" % (key.lower(), field), "symbol": key, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    if ps["exchange"] == "NSE" and bands and no_circuit_reason(key, bands) is None:
        out.append({"id": "%s-circuit" % key.lower(), "symbol": key, "type": "circuit"})
    if product == "MIS":
        flat = plan.get("flat_by")
        if not flat:
            try:
                day = datetime.date.fromisoformat(str(plan.get("date")))
            except ValueError:
                day = to_ist(utc_now()).date()
            flat = hm(default_flat_by(day, bool(plan.get("cas_stock")),
                                      dict(cfg or {}, flat_by=cfg_flat_by or (cfg or {}).get("flat_by") or DEFAULT_FLAT_BY))[0])
        if _hhmm(flat) is None:
            raise AlertError("the plan's flat_by %r is not a time like 15:10" % flat)
        rule = {"id": "%s-flat-by" % key.lower(), "symbol": key, "type": "time", "at": flat,
                "label": "flat by", "before_close": True, "product": "MIS"}
        if plan.get("cas_stock"):
            rule["cas_stock"] = True        # on a special session an F&O stock's continuous trading ends earlier
        out.append(rule)
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    if isinstance(pf, dict):
        for p in (pf.get("positions") or []) + (pf.get("holdings") or []):
            if isinstance(p, dict):
                syms.append(str(p.get("ticker") or p.get("symbol") or p.get("tradingsymbol") or "").upper())
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and (plan.get("yahoo") or plan.get("symbol")):
        syms.append(str(plan.get("symbol") or plan.get("yahoo")).upper())
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def cmd_clock(a):
    now = parse_now(a.now)
    cas, cas_note = None, None
    if a.symbol:
        ps = parse_symbol(a.symbol)
        cas, cas_note = cas_status(ps, to_ist(now).date(), offline=a.offline, fo_file=a.fo_lots)
    st = market_status(now, cas)
    if cas_note:
        st["fo_list_note"] = cas_note
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print("India %s (%s): NSE %s, %s." % (st["now_ist"], st["weekday"],
                                              "OPEN" if st["open"] else "closed", st["reason"]))
        if st.get("opens"):
            print("Today's session: %s to %s IST%s." % (st["opens"], st["closes"],
                                                        (" (%s)" % st["special"]) if st.get("special") else ""))
        if st.get("fo_close"):
            print("Closing auction: F&O stocks stop continuous trading at %s, other stocks at %s (the auction takes "
                  "no stop-loss orders)." % (st["fo_close"], st["closes"]))
        if a.symbol:
            if st.get("cas_stock") and st.get("fo_close"):
                print("%s is an F&O stock: continuous trading ends at %s%s." % (
                    a.symbol.upper(), st["continuous_close"],
                    "" if not st["open"] or to_ist(now).time() < _hhmm(st["continuous_close"]) else
                    " and has ended: only the closing auction is left"))
            elif st.get("continuous_close"):
                print("%s trades to %s." % (a.symbol.upper(), st["continuous_close"]))
            if cas_note:
                print(cas_note + ".")
        if not st["open"] and st.get("next_open"):
            print("Next open: %s IST." % st["next_open"])
        if st.get("muhurat_note"):
            print(st["muhurat_note"])
        if not st["calendar_known"]:
            print(calendar_note(datetime.date.fromisoformat(st["date"])))
    return 0 if st["open"] else 1


def cmd_quote(a):
    keys = _dedupe([_skey(s) for s in a.symbols] + [_skey(s) for s in (holdings(a) if a.holdings else [])])
    if not keys:
        raise AlertError("which tickers? alerts.py quote RELIANCE TCS, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(keys, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in keys:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1),
                                                           inr(q["prev_close"]))
                print("%-10s %s%s  %s  [%s]" % (s, inr(q["price"]), chg, q.get("time", ""), q["source"]))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are free, delayed and unofficial: for alerts only. Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    for r in rules:
        validate(r)
    now = parse_now(a.now)
    status = market_status(now)
    watched = [r for r in rules if active(r, status["date"])]
    keys = _dedupe([_skey(r.get("symbol", "")) for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(keys, a.offline) if keys else ({}, []))
    bands = None
    if any(r.get("type") == "circuit" for r in watched):
        try:
            bands, warning = get_bands(a.bands_cache, now, a.offline)
            if warning:
                errors.append(warning)
        except AlertError as e:
            errors.append(str(e))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now, bands)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        print("NSE %s (%s IST, %s)." % ("OPEN" if status["open"] else "closed", status["now_ist"], status["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %s" % (s, inr(quotes[s]["price"])) for s in keys if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. Check your stop order and open positions with your broker "
                  "(or ask nse-broker), and close any MIS position yourself before the broker's automatic "
                  "square-off from about 15:20. To close one: nse-broker builds the sell ticket, then nse-risk "
                  "and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run nse-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        ps = parse_symbol(plan.get("yahoo") or plan.get("symbol") or "", str(plan.get("exchange") or "NSE").upper())
        bands, why = None, None
        if ps["exchange"] == "NSE":
            try:
                bands, warning = get_bands(a.bands_cache, parse_now(a.now), a.offline)
            except AlertError as e:
                why = "%s: no circuit alert was set" % e
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"), bands, cfg)
        if ps["exchange"] == "NSE" and not any(r["type"] == "circuit" for r in new):
            why = why or (no_circuit_reason(ps["tradingsymbol"], bands) + ".")
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for k in list(state["rung"]):
                if k.split(":")[0] in ids:
                    del state["rung"][k]
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        if why:
            print("Note: " + why)
        if str(plan.get("product") or "MIS").upper() == "CNC":
            print("CNC (delivery) plan: no flat-by alert, the position is held past today.")
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free quotes are delayed; the stop order at the broker is what actually stops you out.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an India time like 15:10")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = _skey(a.symbol)
        if a.before_close:
            rule["before_close"] = True
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = _skey(a.symbol)
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        elif a.circuit:
            if parse_symbol(sym)["exchange"] == "NSE":
                bands, _ = get_bands(a.bands_cache, parse_now(a.now), a.offline)
                why = no_circuit_reason(sym, bands)
                if why:
                    raise AlertError(why.replace("so no circuit alert was set", "so a circuit alert cannot be set"))
            rule = {"type": "circuit"}
            if a.within is not None:
                rule["within"] = a.within
            if a.side:
                rule["side"] = a.side
        else:
            raise AlertError("give one of --above, --below, --stop, --target, --pct or --circuit")
        val = rule.get("price", rule.get("pct", rule.get("within")))
        rid = a.id or ("%s-%s%s" % (sym.lower(), rule["type"], ("-" + _n(val)) if val is not None else ""))
        rule = dict({"id": rid}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        rung = rung or state.get("rung", {}).get(rid + ":upper") or state.get("rung", {}).get(rid + ":lower")
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    rung = state.get("rung", {})
    hit = [k for k in rung if k == a.id or k.split(":")[0] == a.id]
    if not hit:
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    for k in hit:
        del rung[k]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (nse-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read yahoo-<KEY>.json and sec_list.csv files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = India time)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is the NSE open?")
    p.add_argument("--symbol", help="a stock, to show when its continuous trading ends (F&O stocks: 15:15)")
    p.add_argument("--fo-lots", metavar="PATH", help="fo_mktlots.csv; default: NSE's file cached in nse/state")
    p.add_argument("--offline", action="store_true", help="use the test copy of the F&O list, no network")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add tickers from your portfolio and plan")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    p.add_argument("--plan", default="nse/plan.json")
    p.add_argument("--write", nargs="?", const="nse/prices.json", metavar="PATH",
                   help="save to prices.json (default nse/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="nse/alerts.json")
    p.add_argument("--state", default="nse/state/alerts.json")
    p.add_argument("--prices-out", default="nse/prices.json")
    p.add_argument("--bands-cache", default="nse/state/sec_list.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="nse/alerts.json")
    p.add_argument("--state", default="nse/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--circuit", action="store_true", help="ring near the upper or lower circuit (NSE stocks)")
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, India time")
    g.add_argument("--from-plan", action="store_true", help="stop, target, circuit and flat-by from the plan")
    p.add_argument("--plan", default="nse/plan.json")
    p.add_argument("--config", default="nse/config.json", help="flat_by when the plan has none")
    p.add_argument("--bands-cache", default="nse/state/sec_list.json")
    p.add_argument("--offline", metavar="DIR", help="read sec_list.csv from DIR, no network")
    p.add_argument("--now", help="pretend it is this time (no offset = India time)")
    p.add_argument("--within", type=float, help="circuit: ring within this percent of the limit")
    p.add_argument("--side", choices=["upper", "lower"], help="circuit: one side only")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by\"")
    p.add_argument("--before-close", action="store_true",
                   help="a time alert that moves to 5 minutes before the close on a special session (Muhurat)")
    p.add_argument("--date", help="watch this rule on this India date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="nse/alerts.json")
    p.add_argument("--state", default="nse/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="nse/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        load_calendar()
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
