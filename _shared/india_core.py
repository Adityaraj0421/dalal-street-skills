"""india_core: the Indian-market layer every nse-* script carries a copy of.

Skills are copied as standalone folders, so each script pastes the parts of
this file it needs (the same way the US pack carries its own NYSE calendar).
This file is the single source of truth: change it here, then re-paste.
Standard library only. Python 3.8+.

Sections: console, time (IST), NSE calendar, symbols, Yahoo, money, ticks,
charges.
"""

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
