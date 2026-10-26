#!/usr/bin/env python3
"""Order tickets, the paper ledger and the decision log for nse-broker. Standard library only.

This tool never talks to a broker and never places a real order. Zerodha has
no paper trading, so in the default PAPER mode it keeps its own ledger:
a simulated fill at your limit price, the stop and target tracked, P&L net of
charges, all in nse/state/broker.json and nse/portfolio.json. In LIVE mode it
only prints the broker's own order payload (one renderer per broker) for Claude to send through that broker's
MCP server or API, or a manual ticket, after your yes.

  show      renders each proposed order as a plain-English ticket (and, for live mode, the payload the
            broker profile in config "broker" renders from the broker-neutral order). An order is
            READY only if nse-risk passed THIS exact order today (matched by
            fingerprint) and nobody has decided on it yet.
  approve   records your explicit yes for ONE order, with your reply word for
            word. It refuses "yes to all", "sure", "ok", a yes that names a
            different stock or amount, and any order that is not READY.
            PAPER: also books the simulated fill. LIVE: prints the entry JSON.
  decline   records a no.
  result    (live) records what happened after the Allow once / Deny card:
            placed (with the broker's order id and the fill), denied or failed.
  legs      (live) prints the protective orders after the entry filled: the
            SL-M stop for MIS, the GTT OCO for CNC (and the optional target).
  fill      records a fill in nse/portfolio.json (cash, shares, charges, P&L).
  track     (paper) checks open positions against the stop and the target,
            from --price, nse/prices.json or Yahoo's 5-minute bars, books the
            exit that would have happened, and lists MIS positions past flat_by.
  flatten   builds a sell ticket that closes a symbol's open position by
            flat_by. It still goes through nse-risk and your yes.
  log       prints today's decisions and the realised P&L.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py result --index 1 --status placed --broker-id 2610260000123 --filled-qty 118 --filled-price 1196.10
    python3 ticket.py legs --index 1
    python3 ticket.py track --price RELIANCE=1211.5
    python3 ticket.py flatten --symbol RELIANCE --product MIS
    python3 ticket.py log

Everything is written to nse/state/broker.json (append only). Dates are
India dates; --now "2026-10-26 09:45" pins the clock (IST).
"""

import argparse
import hashlib

# ---- india_core (pasted verbatim from _shared/india_core.py; do not edit here) ----
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

# ---- end india_core ----


ORDER_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
                "order_type", "price", "trigger_price", "validity")
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
VALIDITY_WORDS = {"DAY": "valid today", "IOC": "immediate or cancel", "TTL": "valid for a few minutes"}
PRODUCT_WORDS = {"MIS": "MIS intraday", "CNC": "CNC delivery"}
DEFAULT_LEV = 5


class TicketError(IndiaError):
    pass


def ist_iso(now_utc):
    """naive UTC -> "2026-10-26T09:45:00+05:30" (India wall clock with its offset)."""
    return to_ist(now_utc).replace(microsecond=0).isoformat() + "+05:30"


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a decision belongs to: its "date" (the day it was made under), else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

NEUTRAL_FIELDS = ("symbol", "exchange", "product", "side", "qty", "order_type", "price", "stop_trigger",
                  "target_price")


def fingerprint(order):
    """Same function as nse-risk/check.py and nse-budget/size.py: identifies the exact order.
    sha1 of the broker-neutral fields, the entry leg, the stop leg (or the GTT OCO) and the target price."""
    tgt = order.get("target") or {}
    core = {"entry": order.get("entry"), "stop": order.get("stop"), "gtt_oco": order.get("gtt_oco"),
            "target": tgt.get("price"),
            "neutral": {k: order.get(k) for k in NEUTRAL_FIELDS if order.get(k) is not None} or None}
    core = {k: v for k, v in core.items() if v is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else repr(f)
    return str(v)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _p(v):
    return "%.2f" % float(v)


def tool_input(leg):
    """The exact arguments for the Kite place_order tool: only the order fields, numbers as numbers."""
    return {k: leg[k] for k in ORDER_FIELDS if k in leg and leg[k] is not None}


def entry_of(order):
    """The entry leg; for a broker-neutral order without rendered legs, one built from the neutral fields."""
    e = order.get("entry")
    if isinstance(e, dict) and e:
        return e
    if order.get("side") and order.get("qty") is not None:
        return {"variety": "regular", "exchange": order.get("exchange") or "NSE", "tradingsymbol": order.get("symbol"),
                "transaction_type": str(order["side"]).upper(), "quantity": order["qty"],
                "product": order.get("product"), "order_type": order.get("order_type") or "LIMIT",
                "price": order.get("price"), "validity": "DAY"}
    return {}


def neutral_payload(order):
    """The broker-neutral fields of an order (what any broker profile renders from)."""
    e = entry_of(order)
    out = {"exchange": e.get("exchange"), "tradingsymbol": symbol_of(order), "side": side_of(order).upper(),
           "quantity": e.get("quantity"), "product": product_of(order),
           "order_type": e.get("order_type"), "price": e.get("price"),
           "stop_trigger": stop_of(order), "target_price": target_of(order)}
    return {k: v for k, v in out.items() if v is not None}


def qty_of(order):
    return _f(entry_of(order).get("quantity"))


def product_of(order):
    return str(entry_of(order).get("product") or order.get("product") or "MIS").upper()


def side_of(order):
    return str(entry_of(order).get("transaction_type", "")).lower()


def symbol_of(order):
    return str(entry_of(order).get("tradingsymbol") or order.get("symbol") or "").upper()


def stop_of(order):
    """The stop price: the SL-M trigger, else the GTT's lower trigger, else the plan's stop."""
    s = order.get("stop")
    if isinstance(s, dict) and _f(s.get("trigger_price")):
        return _f(s["trigger_price"])
    g = order.get("gtt_oco")
    if isinstance(g, dict) and isinstance(g.get("trigger_values"), list) and g["trigger_values"]:
        return _f(g["trigger_values"][0])
    return _f((order.get("plan") or {}).get("stop"))


def target_of(order):
    t = order.get("target")
    if isinstance(t, dict) and _f(t.get("price")):
        return _f(t["price"])
    g = order.get("gtt_oco")
    if isinstance(g, dict) and isinstance(g.get("trigger_values"), list) and len(g["trigger_values"]) == 2:
        return _f(g["trigger_values"][1])
    return _f((order.get("plan") or {}).get("target"))


def max_loss(order, cfg=None):
    """(rupees, stop): lost if the stop fills exactly at the stop, charges included, or (None, stop)."""
    stop = stop_of(order)
    q, e = qty_of(order), entry_of(order)
    if not stop or not q or side_of(order) != "buy":
        return None, stop
    if _f(order.get("max_loss")) is not None and str(e.get("order_type", "")).upper() != "MARKET":
        return round(_f(order["max_loss"]), 2), stop
    entry = _f(e.get("price")) or _f((order.get("plan") or {}).get("entry"))
    if not entry:
        return None, stop
    ch = trade_charges(q * entry, q * stop, product_of(order), str(e.get("exchange") or "NSE"), cfg)["total"]
    return round(q * (entry - stop) + ch, 2), stop


def plain_parts(order, cfg=None):
    """The ticket in plain words, one part per line."""
    e = entry_of(order)
    side = str(e.get("transaction_type", "")).upper()
    sym = symbol_of(order) or "?"
    typ = str(e.get("order_type", "MARKET")).upper()
    prod = product_of(order)
    q = _num(e.get("quantity"))
    if typ == "LIMIT":
        how = "LIMIT %s" % _p(e["price"])
    elif typ == "SL":
        how = "SL trigger %s limit %s" % (_p(e["trigger_price"]), _p(e["price"]))
    elif typ == "SL-M":
        how = "SL-M trigger %s" % _p(e["trigger_price"])
    else:
        how = "MARKET"
    parts = ["%s %s %s (%s), %s, %s" % (side, q, sym, PRODUCT_WORDS.get(prod, prod), how,
                                         VALIDITY_WORDS.get(str(e.get("validity", "DAY")).upper(), e.get("validity")))]
    if isinstance(order.get("stop"), dict):
        parts.append("stop-loss SL-M trigger %s (a SELL placed after the fill)" % _p(order["stop"]["trigger_price"]))
    g = order.get("gtt_oco")
    if isinstance(g, dict) and isinstance(g.get("trigger_values"), list) and len(g["trigger_values"]) == 2:
        parts.append("GTT OCO after the fill: sell at the stop %s or the target %s, whichever comes first"
                     % (_p(g["trigger_values"][0]), _p(g["trigger_values"][1])))
    elif isinstance(order.get("target"), dict) and _f(order["target"].get("price")):
        parts.append("target %s" % _p(order["target"]["price"]))
    loss, stop = max_loss(order, cfg)
    if loss is not None:
        parts.append("max loss about %s if the stop fills at %s, charges included (it can fill worse in a fast market)"
                     % (inr(loss), _p(stop)))
    if order.get("flat_by") and prod == "MIS":
        parts.append("flat by %s IST" % order["flat_by"])
    return parts


def plain(order, cfg=None):
    """BUY 118 RELIANCE (MIS intraday), LIMIT 1196.10, valid today / stop-loss ... / target 1211.30 / max loss ..."""
    return " / ".join(plain_parts(order, cfg))


def amount_numbers(order, cfg=None):
    nums = []
    e = entry_of(order)
    for k in ("quantity", "price", "trigger_price"):
        v = _f(e.get(k))
        if v is not None:
            nums.append(v)
    for v in (stop_of(order), target_of(order)):
        if v is not None:
            nums.append(v)
    loss, _ = max_loss(order, cfg)
    if loss is not None:
        nums.append(loss)
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = symbol_of(order)
    tokens = set(re.findall(r"[A-Z][A-Z0-9&_\-]*", raw.upper()))
    others = {s.upper() for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (", ".join(_num(n) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today and rec.get("status") in (
                "approved", "declined", "placed", "denied", "failed"):
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "nse-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by nse-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for the place_order call" % rec.get("at"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: nse-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    """entry or exit, as nse-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if side_of(order) == "buy" else "exit"


def is_paper(cfg):
    return bool(cfg.get("paper", True))

# ---------------------------------------------------------------- one ticket renderer per broker
# The neutral order (side, qty, product, order type, price, trigger) becomes the payload of that broker's order
# API. Field names come from docs/brokers.md. Each renderer returns (tool, payload, notes):
# payload None = a manual ticket (type it into the broker's app). A value in <angle brackets> is something this
# tool cannot know (an instrument token, an ISIN): look it up in the broker's instrument master. "notes" say what
# is verified and what is not: show them to the user. Never invent a tool name: read the connected server's list.

SEBI_NOTE = ("SEBI's retail-algo rules apply to any order API: calls come from a static IP registered with your "
             "broker, at most 10 orders a second, the broker attaches an Algo ID, and a human approves every order "
             "(you do, here).")

# what the broker's official MCP server can do for orders (see docs/brokers.md section 4)
MCP_ORDERS = {
    "groww": ("places", "Groww's official MCP server (https://mcp.groww.in/mcp, OAuth in the browser) places orders: "
                        "read its tool list for the order tool and map the ticket's fields onto it."),
    "zerodha": ("gtt_only", "Zerodha's hosted MCP (https://mcp.kite.trade/mcp) is read-only plus GTT. Regular orders "
                            "need a self-hosted kite-mcp-server with your own Kite Connect key; otherwise place this "
                            "ticket by hand in Kite."),
    "upstox": ("read_only", "Upstox's official MCP server is read-only and cannot place orders. Use Upstox's official "
                            "API (you set it up, with a static IP) or place this ticket by hand in the app."),
    "kotak": ("read_only", "Kotak Neo's official MCP server cannot place orders. Use the Neo Trade API (you set it "
                           "up) or place this ticket by hand in the app."),
    "hdfcsky": ("unknown", "HDFC Sky has an official MCP server (https://api.hdfcsky.com/mcp) but order placement is "
                           "not confirmed: read its tool list. Otherwise place this ticket by hand."),
}
MCP_NONE = ("none", "%s has no official MCP server. Place this ticket by hand in the app, or through its official "
                    "API if you have set that up (static IP, daily login).")


def live_route(prof):
    """(kind, one sentence) on how a live ticket for this broker can reach the market."""
    if prof["key"] == "sbi":
        return "manual", "SBI Securities has no public retail API and no MCP: type this ticket into the SBI app by hand."
    if prof["key"] in MCP_ORDERS:
        return MCP_ORDERS[prof["key"]]
    return MCP_NONE[0], MCP_NONE[1] % prof["name"]


def _n(order):
    """The neutral fields of an order."""
    e = entry_of(order)
    return {"sym": symbol_of(order), "exch": str(e.get("exchange") or "NSE").upper(), "side": side_of(order).upper(),
            "qty": int(qty_of(order) or 0), "prod": product_of(order),
            "otype": str(e.get("order_type") or "LIMIT").upper(), "price": _f(e.get("price")),
            "trigger": _f(e.get("trigger_price")), "stop": stop_of(order), "target": target_of(order),
            "id": order.get("id") or ""}


def _leg(n, role, q=None):
    """A leg in neutral terms: role entry / stop / target."""
    if role == "entry":
        return {"role": role, "side": n["side"], "qty": n["qty"], "otype": n["otype"], "price": n["price"],
                "trigger": n["trigger"]}
    q = q or n["qty"]
    if role == "stop":
        return {"role": role, "side": "SELL", "qty": q, "otype": "SL-M", "price": None, "trigger": n["stop"]}
    return {"role": role, "side": "SELL", "qty": q, "otype": "LIMIT", "price": n["target"], "trigger": None}


def _clean(d):
    return {k: v for k, v in d.items() if v is not None}


def _s(x):
    return None if x is None else ("%.2f" % x)


def r_zerodha(order, n, L):
    if L["role"] == "entry" and isinstance(order.get("entry"), dict) and order["entry"]:
        p = tool_input(order["entry"])
    elif L["role"] == "stop" and isinstance(order.get("stop"), dict):
        p = tool_input(dict(order["stop"], quantity=L["qty"]))
    else:
        p = _clean({"variety": "regular", "exchange": n["exch"], "tradingsymbol": n["sym"],
                    "transaction_type": L["side"], "quantity": L["qty"], "product": n["prod"],
                    "order_type": L["otype"], "price": L["price"], "trigger_price": L["trigger"], "validity": "DAY"})
    notes = ["Kite Connect v3 place_order fields [verified in the Kite docs]."]
    if L["otype"] in ("MARKET", "SL-M"):
        notes.append("Kite accepts market_protection (-1 = automatic) on MARKET and SL-M orders; add it if the "
                     "server asks.")
    return "place_order", p, notes


def r_groww(order, n, L):
    ot = {"LIMIT": "LIMIT", "MARKET": "MARKET", "SL-M": "SL_M"}[L["otype"]]
    p = _clean({"trading_symbol": n["sym"], "quantity": L["qty"], "price": L["price"],
                "trigger_price": L["trigger"], "validity": "DAY", "exchange": n["exch"], "segment": "CASH",
                "product": n["prod"], "order_type": ot, "transaction_type": L["side"]})
    return "place_order", p, ["Groww Trade API place_order fields [verified in its docs]. The official MCP's own "
                              "parameter names may differ: read its tool list."]


def r_angelone(order, n, L):
    ot = {"LIMIT": "LIMIT", "MARKET": "MARKET", "SL-M": "STOPLOSS_MARKET"}[L["otype"]]
    p = {"variety": "STOPLOSS" if L["otype"] == "SL-M" else "NORMAL", "tradingsymbol": n["sym"] + "-EQ",
         "symboltoken": "<symboltoken of %s: look it up in SmartAPI's instrument master>" % n["sym"],
         "transactiontype": L["side"], "exchange": n["exch"], "ordertype": ot,
         "producttype": {"MIS": "INTRADAY", "CNC": "DELIVERY"}[n["prod"]], "duration": "DAY",
         "price": _s(L["price"]) or "0", "triggerprice": _s(L["trigger"]) or "0", "squareoff": "0", "stoploss": "0",
         "quantity": str(L["qty"])}
    return "placeOrder", p, ["SmartAPI placeOrder: values are strings. Field names from the SDK README [verified]; "
                             "the STOPLOSS variety and the STOPLOSS_MARKET / DELIVERY values are UNVERIFIED "
                             "(the docs site would not load): check them in SmartAPI's docs first."]


def r_icicidirect(order, n, L):
    if n["prod"] == "MIS":
        return None, None, ["ICICIdirect's Breeze API does not allow intraday (margin) orders at all, so an MIS "
                            "ticket must be placed by hand in the ICICIdirect app. (A CNC ticket can use Breeze.)"]
    p = _clean({"stock_code": "<ICICI stock code for %s: not the NSE symbol>" % n["sym"], "exchange_code": n["exch"],
                "product": "cash", "action": L["side"].lower(),
                "order_type": "limit" if L["otype"] == "LIMIT" else "stoploss", "quantity": str(L["qty"]),
                "price": _s(L["price"]), "stoploss": _s(L["trigger"]), "validity": "day"})
    return "place_order", p, ["Breeze place_order fields [verified in its docs]. Breeze refuses market orders: use a "
                              "limit price."]


def r_upstox(order, n, L):
    ot = {"LIMIT": "LIMIT", "MARKET": "MARKET", "SL-M": "SL-M"}[L["otype"]]
    p = _clean({"instrument_token": "<instrument key of %s, like NSE_EQ|ISIN: from Upstox's instrument file>" % n["sym"],
                "quantity": L["qty"], "product": {"MIS": "I", "CNC": "D"}[n["prod"]], "validity": "DAY",
                "price": L["price"] if L["price"] is not None else 0, "order_type": ot,
                "transaction_type": L["side"], "trigger_price": L["trigger"] if L["trigger"] is not None else 0,
                "market_protection": -1 if L["otype"] in ("MARKET", "SL-M") else None,
                "tag": "nse-broker"})
    return "POST /v3/order/place", p, ["Upstox v3 place-order fields [verified in its docs]. Send the optional header "
                                       "X-Algo-Name if Upstox gave you an algo name; market_protection 0 is rejected."]


def r_kotak(order, n, L):
    ot = {"LIMIT": "L", "MARKET": "MKT", "SL-M": "SL-M"}[L["otype"]]
    p = _clean({"exchange_segment": "bse_cm" if n["exch"] == "BSE" else "nse_cm", "product": n["prod"],
                "price": _s(L["price"]) or "0", "order_type": ot, "quantity": str(L["qty"]), "validity": "DAY",
                "trading_symbol": n["sym"] + "-EQ", "transaction_type": "B" if L["side"] == "BUY" else "S",
                "amo": "NO", "trigger_price": _s(L["trigger"]) or "0"})
    return "place_order", p, ["Kotak Neo field names come from the ARCHIVED v2 SDK and may differ in the current "
                              "package: UNVERIFIED for today. The -EQ trading symbol form is a guess: check the scrip master."]


def r_hdfcsky(order, n, L):
    return None, None, ["HDFC Sky's Open API field names could not be verified (its developer portal does not "
                        "render), so no payload is shown: type this ticket into the HDFC Sky app, or read the API docs "
                        "yourself."]


def r_dhan(order, n, L):
    ot = {"LIMIT": "LIMIT", "MARKET": "MARKET", "SL-M": "STOP_LOSS_MARKET"}[L["otype"]]
    p = _clean({"dhanClientId": "<your Dhan client id>", "correlationId": (n["id"] or n["sym"])[:25],
                "transactionType": L["side"], "exchangeSegment": "BSE_EQ" if n["exch"] == "BSE" else "NSE_EQ",
                "productType": {"MIS": "INTRADAY", "CNC": "CNC"}[n["prod"]], "orderType": ot, "validity": "DAY",
                "securityId": "<numeric security id of %s: from Dhan's instrument list>" % n["sym"],
                "quantity": L["qty"], "price": L["price"], "triggerPrice": L["trigger"], "disclosedQuantity": 0,
                "afterMarketOrder": False})
    return "POST /v2/orders", p, ["DhanHQ v2 order fields [verified in its docs]. Place, modify and cancel need a "
                                  "whitelisted static IP."]


def r_motilal(order, n, L):
    ot = {"LIMIT": "LIMIT", "MARKET": "MARKET", "SL-M": "STOPLOSS"}[L["otype"]]
    price = L["price"] if L["price"] is not None else L["trigger"]
    p = _clean({"exchange": n["exch"], "symboltoken": "<symboltoken of %s: from Motilal's scrip master>" % n["sym"],
                "buyorsell": L["side"], "ordertype": ot,
                "producttype": {"MIS": "VALUEPLUS", "CNC": "DELIVERY"}[n["prod"]], "orderduration": "DAY",
                "price": price, "triggerprice": L["trigger"], "quantityinlot": L["qty"], "amoorder": "N",
                "algoid": "<the Algo ID your broker gives you>"})
    return "POST /rest/trans/v2/placeorder", p, [
        "Motilal Oswal order fields [verified in its docs]; VALUEPLUS as the intraday product is an interpretation "
        "(UNVERIFIED). It has no SL-M type: the stop is a STOPLOSS order priced at the trigger, which can miss in a "
        "fast fall. quantityinlot should be the share count for cash equity: check."]


def r_sbi(order, n, L):
    return None, None, ["SBI Securities has no public retail API: type this ticket into the SBI app by hand."]


RENDERERS = {"zerodha": r_zerodha, "groww": r_groww, "angelone": r_angelone, "icicidirect": r_icicidirect,
             "upstox": r_upstox, "kotak": r_kotak, "hdfcsky": r_hdfcsky, "dhan": r_dhan, "motilal": r_motilal,
             "sbi": r_sbi}


def render_leg(order, cfg, role, q=None):
    """(tool, payload or None, notes) for one leg in the configured broker's format."""
    prof = broker_profile(cfg)
    n = _n(order)
    return RENDERERS[prof["key"]](order, n, _leg(n, role, q))


def entry_payload(order, cfg):
    """(tool, payload, notes). For a manual ticket the payload is the neutral order."""
    tool, payload, notes = render_leg(order, cfg, "entry")
    if payload is None:
        return None, neutral_payload(order), notes
    return tool, payload, notes


def gtt_text(order, cfg, q):
    """The delivery stop + target as a GTT / OCO in words and, for Zerodha, the contract's GTT JSON."""
    prof = broker_profile(cfg)
    g = order.get("gtt_oco") if isinstance(order.get("gtt_oco"), dict) else None
    stop, tgt = stop_of(order), target_of(order)
    how = {"gtt_oco": "a GTT with an OCO (two-leg) rule", "forever_oco": "a Forever Order of type OCO",
           "gtt": "a GTT (some brokers only offer a single trigger: then place the stop and keep the target as an alert)"
           }.get(prof["stop_cnc"], "a GTT")
    if prof["key"] == "zerodha" and g:
        g2 = json.loads(json.dumps(g))
        for o2 in g2.get("orders", []):
            o2["quantity"] = q
        return g2, ["Zerodha GTT two-leg payload [the contract's shape]. Read the connected server's tool list for the "
                    "GTT tool: do not guess its name."]
    return None, ["%s holds a delivery stop as %s: sell %d %s at the stop trigger %s and at the target %s. Its API "
                  "field names for it are not verified here: type it into the app, or read the broker's GTT docs."
                  % (prof["name"], how, q, order.get("symbol") or symbol_of(order),
                     _p(stop) if stop else "none", _p(tgt) if tgt else "none")]


# ---------------------------------------------------------------- rendering

def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    prof = broker_profile(cfg)
    paper = is_paper(cfg)
    acct = "PAPER ledger, no real money" if paper else "LIVE ACCOUNT, REAL MONEY"
    lines = ["ORDER %d of %d, %s, %s" % (idx, total, acct, prof["name"])]
    for part in plain_parts(order, cfg):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    if order.get("charges_at_stop") is not None:
        lines.append("  charges (government and broker): about %s round trip at the stop, %s at the target"
                     % (inr(order["charges_at_stop"]), inr(order.get("charges_at_target") or 0)))
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    if paper:
        lines.append("  Paper mode: your yes books a simulated fill in the ledger at the limit price. Nothing is")
        lines.append("  sent to %s. A real limit order might not fill; this ledger assumes it does." % prof["name"])
    else:
        kind, route = live_route(prof)
        tool, payload, notes = entry_payload(order, cfg)
        lines.append("  How this reaches the market: " + route)
        if tool:
            lines.append("  Entry payload (%s):" % tool)
        else:
            lines.append("  Manual ticket: type these into the %s app (or its official API):" % prof["name"])
        for l in json.dumps(payload, indent=2).splitlines():
            lines.append("    " + l)
        for nt in notes:
            lines.append("  note: " + nt)
        if isinstance(order.get("stop"), dict):
            lines.append("  After the entry fills, `ticket.py legs` prints the stop (a separate order; no bracket orders,")
            lines.append("  so nothing protects the position until you place it).")
        elif isinstance(order.get("gtt_oco"), dict):
            lines.append("  After the entry fills, `ticket.py legs` prints the delivery stop and target (GTT / OCO).")
        lines.append("  " + SEBI_NOTE)
    if ready:
        lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
        if not paper:
            lines.append("  If Claude sends it through an MCP server, its own card then asks again: Allow once places it,")
            lines.append("  Deny stops it (set the order tool to Needs approval). Read the connected server's tool list;")
            lines.append("  never rely on a tool name this tool did not print.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    loss, stop = max_loss(order, cfg)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order, cfg),
            "fingerprint": fingerprint(order), "paper": is_paper(cfg),
            "broker": broker_profile(cfg)["key"], "broker_name": broker_profile(cfg)["name"],
            "tool": entry_payload(order, cfg)[0], "tool_input": entry_payload(order, cfg)[1],
            "notes": entry_payload(order, cfg)[2], "live_route": live_route(broker_profile(cfg))[1],
            "why": order.get("why", ""),
            "product": product_of(order), "stop_leg": order.get("stop"), "gtt_oco": order.get("gtt_oco"),
            "target": order.get("target"), "max_loss": loss, "stop": stop}


# ---------------------------------------------------------------- fills -> portfolio (the paper ledger)

def _lev(cfg, product):
    return max(float((cfg or {}).get("mis_leverage") or DEFAULT_LEV), 1.0) if product == "MIS" else 1.0


def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, product="MIS", exchange="NSE",
               extras=None, strict=False, at=None):
    """Record a filled order in nse/portfolio.json: cash, shares, margin and charges.

    BUY adds a lot (cost) and takes cash: the margin (value / mis_leverage) for MIS, the full value
    for CNC, plus the buy-side charges. SELL relieves the oldest lots, releases the margin and books
    the realised P&L net of the charges of both legs in portfolio["closed"].
    extras (stop, target, flat_by, order_id) are kept on a BUY position so `track` can follow them.
    strict: refuse a buy that needs more cash than the ledger has (paper mode).
    Returns {"portfolio", "charges", "gross", "net", "margin"}."""
    product = str(product).upper()
    exchange = str(exchange or "NSE").upper()
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = (cfg or {}).get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    symbol = symbol.upper()
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    value = round(qty * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker", "")).upper() == symbol and str(p.get("product") or "CNC").upper() == product:
            pos = p
    out = {"charges": 0.0, "gross": None, "net": None, "margin": 0.0}
    side = side.lower()
    if side == "buy":
        ch = trade_charges(value, 0, product, exchange, cfg)["total"]
        margin = round(value / _lev(cfg, product), 2)
        need = margin + ch
        if strict and float(pf.get("cash", 0)) + 0.005 < need:
            raise TicketError("not enough cash in the paper ledger: needs %s (margin %s + charges %s), has %s"
                              % (inr(need), inr(margin), inr(ch), inr(float(pf.get("cash", 0)))))
        if pos is None:
            pos = {"ticker": symbol, "product": product, "exchange": exchange, "shares": 0.0, "margin": 0.0,
                   "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": value,
                     "price": price, "charges": ch})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        pos["margin"] = round(float(pos.get("margin", 0)) + margin, 2)
        for k, v in (extras or {}).items():
            if v is not None:
                pos[k] = v
        if at:
            pos["opened_at"] = at
        pf["cash"] = round(float(pf.get("cash", 0)) - need, 2)
        out.update(charges=ch, margin=margin)
    else:
        held = float(pos.get("shares", 0)) if pos else 0.0
        if pos is None or held + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s (%s): the portfolio holds %s"
                              % (_num(qty), symbol, product, _num(held)))
        sell_ch = trade_charges(0, value, product, exchange, cfg)["total"]
        left, cost_out, buy_ch_out = qty, 0.0, 0.0
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            frac = take / float(lot["shares"]) if float(lot["shares"]) else 0
            c_part = float(lot["cost"]) * frac
            b_part = float(lot.get("charges", 0)) * frac
            lot["cost"] = round(float(lot["cost"]) - c_part, 2)
            lot["charges"] = round(float(lot.get("charges", 0)) - b_part, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            cost_out += c_part
            buy_ch_out += b_part
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        frac_pos = qty / held
        released = round(float(pos.get("margin", 0)) * frac_pos, 2)
        pos["margin"] = round(float(pos.get("margin", 0)) - released, 2)
        pos["shares"] = round(held - qty, 6)
        gross = round(value - cost_out, 2)
        net = round(gross - buy_ch_out - sell_ch, 2)
        if product == "MIS":
            pf["cash"] = round(float(pf.get("cash", 0)) + released + gross - sell_ch, 2)
        else:
            pf["cash"] = round(float(pf.get("cash", 0)) + value - sell_ch, 2)
        pf.setdefault("closed", []).append({
            "date": date, "ticker": symbol, "product": product, "shares": qty, "sell_price": price,
            "gross": gross, "charges": round(buy_ch_out + sell_ch, 2), "net": net})
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        out.update(charges=sell_ch, gross=gross, net=net, margin=released)
    write_json(portfolio_path, pf)
    out["portfolio"] = pf
    return out


def log_fill(state_path, now, today, symbol, side, qty, price, product, fp=None, res=None, via=None):
    """A fill in the log, so nse-risk and `log` can see what really happened."""
    state = load_state(state_path)
    rec = {"at": ist_iso(now), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
           "product": product, "qty": _num(float(qty)), "price": float(price),
           "plain": "%s %s %s at %s" % (side.upper(), _num(float(qty)), symbol.upper(), _p(price))}
    if fp:
        rec["fingerprint"] = fp
    if res:
        rec["charges"] = res["charges"]
        if res.get("net") is not None:
            rec["pnl_gross"], rec["pnl_net"] = res["gross"], res["net"]
    if via:
        rec["via"] = via
    state.setdefault("orders", []).append(rec)
    write_json(state_path, state)


# ---------------------------------------------------------------- tracking stop and target (paper)

def evaluate_price(pos, price):
    """One price against a position's stop / target -> None or {"hit", "price"}.
    A stop (SL-M) fills at the market once triggered: at the stop, or at this price when it is already lower."""
    stop, target = _f(pos.get("stop")), _f(pos.get("target"))
    if stop and price <= stop:
        return {"hit": "stop", "price": r2(min(stop, price))}
    if target and price >= target:
        return {"hit": "target", "price": target}
    return None


def evaluate_bars(pos, bars, after=None):
    """The first bar (oldest first) that touched the stop or the target.

    bars: [{"t": naive IST datetime, "open", "high", "low", "close"}]; after: only bars starting at or
    after this IST time. A bar that touches both is a stop (the worst case). A bar that OPENS beyond the
    stop fills at its open (a gap), not at the stop."""
    stop, target = _f(pos.get("stop")), _f(pos.get("target"))
    for b in bars:
        if after is not None and b["t"] < after:
            continue
        hit_stop = bool(stop) and b["low"] <= stop
        hit_tgt = bool(target) and b["high"] >= target
        if hit_stop:
            return {"hit": "stop", "price": r2(min(stop, b["open"])), "at": b["t"]}
        if hit_tgt:
            return {"hit": "target", "price": target, "at": b["t"]}
    return None


def bars_from_chart(doc):
    try:
        res = doc["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        off = int(res["meta"].get("gmtoffset", 19800))
    except (KeyError, IndexError, TypeError):
        return []
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        bars.append({"t": utc_of(t) + datetime.timedelta(seconds=off), "open": o, "high": h, "low": l, "close": c})
    return bars


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-10-26")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run nse-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    broker_profile(cfg)                         # an unknown broker is refused here
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    load_calendar()
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket (the entry and the protective stop shown on it), "
          "never the batch. Education and tooling, not financial advice.")
    return 0 if any_ready else 1


def paper_price(order, a, now):
    """The price a paper order fills at: its limit price, else --price, else a fresh nse/prices.json quote."""
    e = entry_of(order)
    if str(e.get("order_type", "")).upper() in ("LIMIT", "SL") and _f(e.get("price")):
        return _f(e["price"]), "your limit price"
    if getattr(a, "price", None):
        return float(a.price), "the --price you gave"
    pdoc = read_json(getattr(a, "prices", None), "prices", required=False) or {}
    sym = symbol_of(order)
    table = {re.sub(r"\.NS$", "", str(k).upper()): v for k, v in (pdoc.get("prices") or {}).items()}
    psym = sym + ".BO" if str(e.get("exchange")).upper() == "BSE" else sym
    p = _f(table.get(psym))
    detail = {re.sub(r"\.NS$", "", str(k).upper()): v for k, v in (pdoc.get("detail") or {}).items()}
    if p:
        d = detail.get(psym) or {}
        t = None
        for key in ("time", "fetched"):
            try:
                t = datetime.datetime.fromisoformat(str(d.get(key)).replace("Z", "+00:00")).replace(tzinfo=None)
                break
            except ValueError:
                continue
        if t is not None and abs((now - t).total_seconds()) <= 15 * 60:
            return p, "the fresh quote in %s" % a.prices
    raise TicketError("a paper %s order at the market needs a price: pass --price P (or refresh nse/prices.json)"
                      % str(e.get("order_type", "MARKET")).upper())


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [symbol_of(o) for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    paper = is_paper(cfg)
    fill_price = how = None
    if paper:
        fill_price, how = paper_price(order, a, now)        # before anything is written
    loss, stop = max_loss(order, cfg)
    kind = kind_of(order, rv)
    fp = fingerprint(order)
    base = {"date": today, "index": a.index, "fingerprint": fp, "symbol": symbol_of(order),
            "side": side_of(order), "qty": entry_of(order).get("quantity"), "product": product_of(order),
            "kind": kind, "max_loss": loss, "stop": stop}
    rec = dict(base, at=ist_iso(now), plain=plain(order, cfg), status="approved", reply=a.reply,
               paper=paper, broker=broker_profile(cfg)["key"])
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order, cfg)))
    if not paper:
        prof = broker_profile(cfg)
        name, payload, notes = entry_payload(order, cfg)
        print("How this reaches the market: " + live_route(prof)[1])
        if name:
            print("Entry payload (%s), exactly:" % name)
        else:
            print("Manual ticket: type these into the %s app (or its official API):" % prof["name"])
        print(json.dumps(payload, indent=2))
        for nt in notes:
            print("note: " + nt)
        print("(Read the connected server's tool list first: if its parameter names differ, map these fields; "
              "never invent a tool name.)")
        print(SEBI_NOTE)
        print("If an MCP card appears, Allow once / Deny. Then: ticket.py result --index %d --status placed|denied|failed"
              % a.index)
        return 0
    # PAPER: book the simulated fill
    e = entry_of(order)
    sym, side, q, prod = symbol_of(order), side_of(order), qty_of(order), product_of(order)
    extras = None
    if side == "buy":
        extras = {"stop": stop_of(order), "target": target_of(order), "flat_by": order.get("flat_by"),
                  "order_id": order.get("id")}
    try:
        res = apply_fill(a.portfolio, cfg, sym, side, q, fill_price, today, prod, e.get("exchange", "NSE"),
                         extras, strict=True, at=ist_iso(now))
    except TicketError:
        state["orders"].append(dict(base, at=ist_iso(now), plain=rec["plain"], status="failed",
                                    note="paper fill refused (see the message)"))
        write_json(a.state, state)
        raise
    state["orders"].append(dict(base, at=ist_iso(now), plain=rec["plain"], status="placed",
                                broker_order_id="PAPER-%s-%d" % (today.replace("-", ""), len(state["orders"]) + 1),
                                note="paper fill"))
    write_json(a.state, state)
    log_fill(a.state, now, today, sym, side, q, fill_price, prod, fp, res, "paper")
    print("PAPER FILL (simulated, nothing sent to Zerodha): %s %s %s at %s (%s), charges %s."
          % (side.upper(), _num(q), sym, inr(fill_price), how, inr(res["charges"])))
    if res["net"] is not None:
        print("Realised: %s before charges, %s net of charges for the round trip." % (inr(res["gross"], sign=True),
                                                                                    inr(res["net"], sign=True)))
    elif extras and (extras.get("stop") or extras.get("target")):
        print("Tracking stop %s and target %s. Check them with: ticket.py track"
              % (_p(extras["stop"]) if extras.get("stop") else "none",
                 _p(extras["target"]) if extras.get("target") else "none"))
        if prod == "MIS" and order.get("flat_by"):
            print("Flat by %s IST: ticket.py flatten --symbol %s then closes it." % (order["flat_by"], sym))
    print("Cash in the paper ledger: %s." % inr(res["portfolio"]["cash"]))
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": symbol_of(order), "side": side_of(order), "product": product_of(order),
           "plain": plain(order, cfg), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order, cfg)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    if is_paper(cfg):
        raise TicketError("this is the paper ledger: `approve` already booked the fill. `result` is for live orders")
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "product": rec.get("product"), "plain": rec["plain"], "status": a.status}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    prod = product_of(order)
    if a.status == "placed" and a.filled_qty and a.filled_price:
        extras = None
        if side_of(order) == "buy":
            extras = {"stop": stop_of(order), "target": target_of(order), "flat_by": order.get("flat_by"),
                      "order_id": order.get("id")}
        res = apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today, prod,
                         entry_of(order).get("exchange", "NSE"), extras, at=ist_iso(now))
        log_fill(a.state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, prod, fp, res)
        print("Added the fill to %s (charges %s)." % (a.portfolio, inr(res["charges"])))
        if rec.get("kind") == "entry" and (order.get("stop") or order.get("gtt_oco")):
            print("The position has NO stop at the broker yet. Now: ticket.py legs --index %d" % a.index)
    elif a.status == "placed":
        print("When it fills, record it: ticket.py result again with --filled-qty Q --filled-price P, or "
              "ticket.py fill --symbol %s --side %s --product %s --qty Q --price P" % (rec["symbol"], rec["side"], prod))
    return 0


def cmd_legs(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    filled = [r for r in state.get("orders", []) if r.get("fingerprint") == fp and rec_day(r) == today
              and r.get("status") == "filled" and r.get("side") == side_of(order)]
    if a.qty is not None:
        q = _f(a.qty)
    elif filled:
        q = sum(_f(r.get("qty")) or 0 for r in filled)
    else:
        raise TicketError("no fill recorded for order %d today: record the entry fill first "
                          "(ticket.py result --filled-qty Q --filled-price P) or pass --qty Q" % a.index)
    if not q or q <= 0:
        raise TicketError("--qty wants the number of shares that filled")
    q = int(q)
    sym, prod = symbol_of(order), product_of(order)
    prof = broker_profile(cfg)
    kind, route = live_route(prof)
    if isinstance(order.get("stop"), dict) or (prod == "MIS" and stop_of(order)):
        tool, payload, notes = render_leg(order, cfg, "stop", q)
        print("Protective stop for the %d %s shares that filled (SELL, stop-loss market). Place it NOW, before "
              "anything else:" % (q, sym))
        if payload is None:
            print("Manual ticket: in the %s app place a SELL of %d %s (%s) as a stop-loss at trigger %s."
                  % (prof["name"], q, sym, prod, _p(stop_of(order))))
        else:
            print("Payload (%s):" % tool)
            print(json.dumps(payload, indent=2))
        for nt in notes:
            print("note: " + nt)
        print("It is the stop shown on the ticket you said yes to; any MCP card still appears.")
        if _f(target_of(order)) and payload is not None:
            ttool, tpayload, _ = render_leg(order, cfg, "target", q)
            print("")
            print("Optional target (a LIMIT sell). There is no OCO for an MIS stop: if you place both, the one that")
            print("does not fill stays open, and filling it later would open a SHORT position. Cancel the other")
            print("one the moment one fills, or skip this and use an nse-alerts target alert instead:")
            print(json.dumps(tpayload, indent=2))
        print("")
        print("Route: " + route)
        print("Read the connected server's tool list for the exact order tool; do not guess a name.")
        print(SEBI_NOTE)
    elif isinstance(order.get("gtt_oco"), dict) or (prod == "CNC" and stop_of(order)):
        g, notes = gtt_text(order, cfg, q)
        print("Delivery stop and target for the %d %s shares that filled. Place it after the entry fills:" % (q, sym))
        if g is not None:
            print(json.dumps(g, indent=2))
        for nt in notes:
            print("note: " + nt)
        print("A GTT trigger sends a LIMIT order when the price touches it; in a gap past the stop that limit can "
              "stay unfilled.")
        print("Route: " + route)
        print(SEBI_NOTE)
    else:
        print("This order has no protective leg (no stop, no GTT). Nothing to place.")
        return 1
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    broker_profile(cfg)
    now = parse_now(a.now)
    date = a.date or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-10-26")
    extras = {"stop": _f(a.stop), "target": _f(a.target), "flat_by": a.flat_by} if a.side == "buy" else None
    res = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date, a.product, a.exchange, extras,
                     at=ist_iso(now))
    log_fill(a.state, now, date, a.symbol, a.side, a.qty, a.price, a.product, None, res, "manual")
    print("Recorded %s %s %s (%s) at %s on %s in %s. Charges %s. Cash now %s." % (
        a.side, a.qty, a.symbol.upper(), a.product, inr(float(a.price)), date, a.portfolio, inr(res["charges"]),
        inr(res["portfolio"]["cash"])))
    if res["net"] is not None:
        print("Realised: %s before charges, %s net of charges." % (inr(res["gross"], sign=True), inr(res["net"], sign=True)))
    return 0


def cmd_track(a):
    cfg = read_json(a.config, "config", required=False) or {}
    broker_profile(cfg)
    now, today = _clock(a)
    load_calendar()
    pf = read_json(a.portfolio, "portfolio", required=False) or {}
    manual = {}
    for part in (a.price or []):
        for kv in part.split(","):
            if "=" in kv:
                s, v = kv.split("=", 1)
                manual[parse_symbol(s)["tradingsymbol"]] = float(v)
    pdoc = read_json(a.prices, "prices", required=False) or {}
    quotes = {re.sub(r"\.NS$", "", str(k).upper()): _f(v) for k, v in (pdoc.get("prices") or {}).items()}
    detail = {re.sub(r"\.NS$", "", str(k).upper()): v for k, v in (pdoc.get("detail") or {}).items()}
    now_ist = to_ist(now)
    positions = [p for p in pf.get("positions", []) if p.get("stop") or p.get("target") or str(p.get("product")) == "MIS"]
    if not positions:
        print("No open positions with a stop or target to track in %s." % a.portfolio)
        return 0
    state = load_state(a.state)
    hit_any = False
    for pos in list(positions):
        sym, prod = str(pos["ticker"]).upper(), str(pos.get("product") or "CNC").upper()
        exch = pos.get("exchange", "NSE")
        qk = sym + ".BO" if str(exch).upper() == "BSE" else sym        # the key in prices.json
        res, src = None, None
        if sym in manual:
            res, src = evaluate_price(pos, manual[sym]), "your --price %s" % _p(manual[sym])
        elif quotes.get(qk):
            d = detail.get(qk) or {}
            qt = None
            for key in ("time", "fetched"):
                try:
                    qt = datetime.datetime.fromisoformat(str(d.get(key)).replace("Z", "+00:00")).replace(tzinfo=None)
                    break
                except ValueError:
                    continue
            if qt is not None and abs((now - qt).total_seconds()) <= 15 * 60:
                res, src = evaluate_price(pos, quotes[qk]), "the quote %s in %s" % (_p(quotes[qk]), a.prices)
        if src is None and not a.offline:
            try:
                doc = json.loads(yahoo_get(chart_path(pos.get("yahoo") or (sym + (".BO" if exch == "BSE" else ".NS")),
                                                      "1d", "5m")))
                bars = bars_from_chart(doc)
                after = None
                if pos.get("opened_at"):
                    try:
                        after = datetime.datetime.fromisoformat(pos["opened_at"]).replace(tzinfo=None)
                    except ValueError:
                        after = None
                res, src = evaluate_bars(pos, bars, after), "Yahoo 5-minute bars since the entry"
                if not bars:
                    src = None
            except (IndiaError, ValueError):
                src = None
        if src is None:
            print("%s %s: no price to check against (give --price %s=P, refresh nse/prices.json, or drop --offline)."
                  % (sym, prod, sym))
        elif res is None:
            print("%s %s: stop %s and target %s not touched (%s)." % (
                sym, prod, _p(pos["stop"]) if pos.get("stop") else "none",
                _p(pos["target"]) if pos.get("target") else "none", src))
        else:
            hit_any = True
            q = float(pos["shares"])
            why = {"stop": "STOP hit", "target": "TARGET hit"}[res["hit"]]
            out = apply_fill(a.portfolio, cfg, sym, "sell", q, res["price"], today, prod, exch, at=ist_iso(now))
            log_fill(a.state, now, today, sym, "sell", q, res["price"], prod, None, out, "paper " + res["hit"])
            print("%s %s: %s (%s): sold %s at %s in the paper ledger. %s before charges, %s net after all charges "
                  "(exit-side charges %s)."
                  % (sym, prod, why, src, _num(q), inr(res["price"]), inr(out["gross"], sign=True),
                     inr(out["net"], sign=True), inr(out["charges"])))
            if res["hit"] == "stop":
                print("   A real SL-M fills at the next market price after the trigger: it can be worse than this.")
    pf = read_json(a.portfolio, "portfolio", required=False) or {}
    fb_default = cfg.get("flat_by") or "15:10"
    for pos in pf.get("positions", []):
        if str(pos.get("product")) != "MIS":
            continue
        sym = str(pos["ticker"]).upper()
        opened = str(pos["lots"][0].get("date")) if pos.get("lots") else today
        fb = pos.get("flat_by") or fb_default
        if opened < today:
            print("%s MIS: opened %s and still open. A real MIS position is squared off the same day: close it "
                  "in the ledger (ticket.py flatten --symbol %s)." % (sym, opened, sym))
        else:
            try:
                past = now_ist.time().replace(second=0, microsecond=0) >= parse_hhmm(fb, "flat_by")
            except IndiaError:
                past = False
            if past:
                print("%s MIS: past its flat_by %s IST and still open. Close it now: ticket.py flatten --symbol %s "
                      "(%s squares off MIS from about %s with a fee)." % (sym, fb, sym, broker_profile(cfg)["name"],
                                                                         hm(squareoff_time(now_ist.date(), True, cfg)[0])))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    broker_profile(cfg)
    sp = parse_symbol(a.symbol)
    sym, exch = sp["tradingsymbol"], sp["exchange"]
    prod = a.product
    now, today = _clock(a)
    load_calendar()
    source = "the broker's positions"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0 or abs(qty - round(qty)) > 1e-9:
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 118")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if str(p.get("ticker") or p.get("symbol") or "").upper() == sym
                  and str(p.get("product") or "CNC").upper() == prod)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s %s shares in %s. Check the broker's positions and pass "
                              "--qty if you do hold some" % (sym, prod, a.portfolio))
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == sym else None) \
        or cfg.get("flat_by") or "15:10"
    leg = {"variety": "regular", "exchange": exch, "tradingsymbol": sym, "transaction_type": "SELL",
           "quantity": int(qty), "product": prod, "order_type": "MARKET", "validity": "DAY"}
    if a.limit_price:
        leg.update(order_type="LIMIT", price=round(float(a.limit_price), 2))
    order = {"id": "%s-%s-%s-FLAT" % (sym, today.replace("-", ""), to_ist(now).strftime("%H%M")),
             "symbol": sym, "exchange": exch, "yahoo": sp["yahoo"], "series": "EQ", "product": prod,
             "side": "SELL", "qty": int(qty), "order_type": leg["order_type"], "price": leg.get("price"),
             "stop_trigger": None, "target_price": None, "broker": broker_profile(cfg)["key"],
             "entry": leg, "stop": None, "target": None, "gtt_oco": None,
             "plan": {}, "flat_by": flat_by if prod == "MIS" else None,
             "why": "flat by %s IST: closes the %s %s position (qty from %s)" % (flat_by, sym, prod, source)}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "nse-broker", "created_at": iso(now), "status": "proposed", "paper": is_paper(cfg),
           "note": "Flatten ticket. Nothing is placed until nse-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s IST)." % (sym, plain(order, cfg), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against the broker's positions before you say yes." % source)
    if not a.limit_price:
        print("It is a MARKET order. If your broker's API insists on a limit price, re-run with --limit-price P.")
    print("Next:")
    print("  1. nse-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    if is_paper(cfg):
        print("  3. Paper: the yes books the exit in the ledger (give --price P to approve if there is no fresh quote).")
    else:
        print("  3. LIVE: cancel the open protective stop first (the broker's cancel call or the MCP server's cancel")
        print("     tool, Needs approval; read its tool list), or both could execute and leave you SHORT. For CNC, delete the GTT.")
        print("     Then place the sell with the ticket, then `result`.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % inr(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        net = (" net %s" % inr(r["pnl_net"], sign=True)) if r.get("pnl_net") is not None else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-9s %s%s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, net, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    pf = read_json(a.portfolio, "portfolio", required=False) or {}
    closed = [c for c in pf.get("closed", []) if a.all or c.get("date") == today]
    if closed:
        g, ch, n = (sum(c[k] for c in closed) for k in ("gross", "charges", "net"))
        print("Realised%s: %s before charges, %s charges, %s net." % ("" if a.all else " today", inr(g, sign=True),
                                                                      inr(ch), inr(n, sign=True)))
    return 0


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Order tickets, paper ledger and your yes (nse-broker). Never places "
                                             "real orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this India time (IST), e.g. \"2026-10-26 09:45\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in India)")

    def common(p):
        p.add_argument("--orders", default="nse/orders.json")
        p.add_argument("--config", default="nse/config.json")
        p.add_argument("--risk", default="nse/state/risk.json")
        p.add_argument("--state", default="nse/state/broker.json")
        p.add_argument("--portfolio", default="nse/portfolio.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order (paper: books the fill)")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p.add_argument("--price", help="paper fill price for a market order")
    p.add_argument("--prices", default="nse/prices.json")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="(live) record what happened after the Allow once / Deny card")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p = sub.add_parser("legs", help="(live) the protective stop / GTT to place after the entry filled")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--qty", help="shares that filled (default: the recorded fill)")
    p = sub.add_parser("fill", help="record a fill in nse/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--product", required=True, type=lambda s: s.upper(), choices=["MIS", "CNC"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--exchange", default="NSE", type=lambda s: s.upper(), choices=["NSE", "BSE"])
    p.add_argument("--stop")
    p.add_argument("--target")
    p.add_argument("--flat-by", dest="flat_by")
    p.add_argument("--date", help="YYYY-MM-DD (default: today in India)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    p.add_argument("--config", default="nse/config.json")
    p.add_argument("--state", default="nse/state/broker.json")
    p = sub.add_parser("track", help="(paper) check open positions against their stop and target")
    p.add_argument("--price", action="append", metavar="SYM=P[,SYM=P]", help="a price to check against")
    p.add_argument("--prices", default="nse/prices.json")
    p.add_argument("--offline", action="store_true", help="do not fetch Yahoo bars")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    p.add_argument("--config", default="nse/config.json")
    p.add_argument("--state", default="nse/state/broker.json")
    clock(p)
    p = sub.add_parser("flatten", help="a sell ticket that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--product", default="MIS", type=lambda s: s.upper(), choices=["MIS", "CNC"])
    p.add_argument("--qty", help="shares you hold, from the broker (default: nse/portfolio.json)")
    p.add_argument("--limit-price", help="make it a LIMIT sell at this price instead of MARKET")
    p.add_argument("--orders", default="nse/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    p.add_argument("--plan", default="nse/plan.json")
    p.add_argument("--config", default="nse/config.json")
    clock(p)
    p = sub.add_parser("log", help="today's decisions and the realised P&L")
    p.add_argument("--state", default="nse/state/broker.json")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "legs": cmd_legs, "fill": cmd_fill, "track": cmd_track, "flatten": cmd_flatten,
                "log": cmd_log}[a.cmd](a)
    except IndiaError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
