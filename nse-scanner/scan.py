#!/usr/bin/env python3
"""Chart Scanner for nse-scanner: scan Nifty charts for setups. Standard library only.

    python3 scan.py universe                          # refresh the Nifty 500 list (cached in nse/state/)
    python3 scan.py universe --universe all           # every NSE EQ-series stock (EQUITY_L.csv)
    python3 scan.py daily                             # after the close / before the open: daily setups
    python3 scan.py daily --universe nifty50          # nifty50 nifty100 nifty200 nifty500 midcap150
                                                      # smallcap250 bank all
    python3 scan.py daily --symbols RELIANCE,TCS,M&M  # or --watchlist FILE, or --limit 200
    python3 scan.py orb                               # after 09:35 IST: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15    # cut the clock at 10:15 IST
    python3 scan.py delivery                          # NSE bhavcopy: delivery % and quantity spikes
    python3 scan.py orb --offline wall5m.json.gz --now "2026-10-23 10:15" --lists DIR

universe  Downloads the stock list (a niftyindices.com constituent list, or NSE's
          EQUITY_L.csv keeping the EQ series) and caches it as
          nse/state/<list>-YYYY-MM-DD.csv. A cache younger than 7 days is reused;
          --refresh-lists forces a download. Stocks in the BE / BZ / SM / ST
          series are not in `--universe all` (trade-for-trade and SME stocks).

daily     Scan of daily CLOSES (Yahoo spark: 20 symbols a request, 3 months of
          1-day bars, closes only). Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the session's close (15:30 IST).
          Yahoo's spark has no volume, so liquidity comes from the NSE bhavcopy
          of the last trading day: --min-turnover-lakh (default 0 = off) in
          rupees lakh traded; the last close must be at least --min-price (10).

orb       Opening-range breakouts, 5-minute bars from 09:15 IST. The range is the
          first --or-minutes (15) of the session. Stage 1: spark closes for every
          symbol; a chart goes on only if a completed bar after the range closed
          at or above the highest close of the range (a necessary condition).
          Stage 2: each candidate's v8 chart (real highs and lows): range high =
          the highest high of the range bars, a hit = the first completed bar to
          CLOSE above it, at that bar's close time. Stage 2 fetches at most
          --max-confirm (200) charts, the most traded first (turnover of the last
          bhavcopy; the strongest close when unknown), and says so when it stops
          short. A chart is usable only when every 5-minute bar from the open to
          the cutoff is there (a missing bar is a halt or no trades).

delivery  The investor scan. The NSE bhavcopy (sec_bhavdata_full_DDMMYYYY.csv)
          of the latest trading day has each stock's delivery quantity and
          delivery %. A stock is listed when, against its OWN average over the
          earlier --history (7) bhavcopies (cached in nse/state/bhav/):
            delivery % is at least the average + --per-rise (10) points, and at
            least --min-delivery-pct (40) %, and
            delivery quantity is at least --qty-mult (2.0) times its average.
          A stock needs --min-history (3) earlier days. High delivery means
          shares were taken home instead of traded out: a sign of holding, not
          of direction. Check the price move and the news (nse-news).

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick) is
dropped, and a bar counts only once it is complete (start + 5 minutes <= the
clock). Times are India time (IST, UTC+5:30). --now pins the clock
("2026-10-23 10:15" is IST; ...Z for UTC).

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on a
429 or 5xx, one retry after a pause on a 429. A polite pause between requests
(--pause). Partial results are reported as partial. NSE's website API
(nseindia.com/api) is never used: only the open archives.nseindia.com files.

--offline PATH reads chart files instead of the network (daily and orb): a
folder of <T>.json v8 chart files, or one file that is a spark response, a v8
chart, or a bundle {"RELIANCE": <v8 json>, ...}. .json.gz is fine. --lists DIR
reads the Nifty lists, EQUITY_L.csv and the bhavcopies from DIR.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
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


# ================================================================ the scanner

ScanError = IndiaError


class NotFound(ScanError):
    """The server answered 404."""


SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
STATE_DIR = "nse/state"
WATCHLIST = "nse/watchlist.json"
CONFIG = "nse/config.json"
HOLIDAYS = "nse/holidays.json"
LIST_MAX_AGE = 7    # days a cached stock list is reused
BHAV_KEEP = 20      # bhavcopy files kept in nse/state/bhav
BHAV_LOOKBACK = 40  # calendar days to walk back looking for bhavcopies
DEFAULT_UNIVERSE = "nifty500"
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."
DELIVERY_NOTE = ("High delivery % means shares were taken home, not traded out: a sign of holding, not of "
                 "direction. Check the price move and the news.")

INDEX_LISTS = {
    "nifty50": "ind_nifty50list", "nifty100": "ind_nifty100list", "nifty200": "ind_nifty200list",
    "nifty500": "ind_nifty500list", "midcap150": "ind_niftymidcap150list",
    "smallcap250": "ind_niftysmallcap250list", "bank": "ind_niftybanklist",
}
LIST_URL = "https://www.niftyindices.com/IndexConstituent/%s.csv"
EQUITY_L_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
BHAV_URL = "https://archives.nseindia.com/products/content/sec_bhavdata_full_%s.csv"
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
    ("delivery", "delivery spike"),
]
LABEL = dict(SETUPS)


# ---------------------------------------------------------------- time

def ts_to_ist(ts):
    """Unix seconds -> naive India time. Never uses the machine's time zone."""
    return to_ist(utc_of(ts))


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    return ampm(t)


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


def parse_hhmm_flag(text, flag="--at"):
    try:
        return parse_hhmm(text, flag)
    except IndiaError:
        raise ScanError("%s wants an India time like 10:15" % flag)


def parse_now_scan(text):
    return parse_now(text)


# ---------------------------------------------------------------- network

def http_text(url, timeout=60):
    """GET a plain file (CSV) from the open archives. 404 -> NotFound."""
    host = urllib.parse.urlsplit(url).netloc
    try:
        return _open_url(url, timeout, accept="*/*")
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        if code == 404:
            raise NotFound("%s has no file at %s (HTTP 404)" % (host, url.rsplit("/", 1)[-1]))
        raise ScanError("%s answered HTTP %d for %s" % (host, code, url.rsplit("/", 1)[-1]))
    except (urllib.error.URLError, OSError) as e:
        raise ScanError("could not reach %s: %s" % (host, getattr(e, "reason", e)))


def spark_path(yahoo_symbols, rng, interval):
    return "/v8/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(yahoo_quote(s) for s in yahoo_symbols), rng, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo v8 chart result -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def series_from_spark(item):
    """One entry of a spark answer: closes only, no volume, no highs or lows."""
    t = item.get("timestamp") or []
    n = len(t)
    c = item.get("close")
    c = list(c) + [None] * (n - len(c)) if isinstance(c, list) else [None] * n
    meta = {k: item.get(k) for k in ("symbol", "chartPreviousClose", "previousClose", "fulldayPrice")}
    return {"t": list(t), "c": c, "o": None, "h": None, "l": None, "v": None, "meta": meta}


def _is_spark_flat(doc):
    return bool(doc) and all(isinstance(v, dict) and "close" in v and "timestamp" in v for v in doc.values())


def parse_spark(text_or_doc):
    """Spark JSON -> {YAHOO_SYMBOL: series}. Yahoo answers {"RELIANCE.NS": {timestamp, close, ...}, ...};
    symbols it does not know are simply missing. Its error answer is {"spark": {"error": ...}}."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    if not isinstance(doc, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    if "spark" in doc:
        sp = doc["spark"] or {}
        err = sp.get("error")
        if err:
            raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
        if not sp.get("result"):
            return {}
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    if "chart" in doc:
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    return {k.upper(): series_from_spark(v) for k, v in doc.items()
            if isinstance(v, dict) and "close" in v and "timestamp" in v}


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _yahoo_of(name):
    try:
        return parse_symbol(name)["yahoo"]
    except IndiaError as e:
        raise ScanError(str(e))


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors (keyed by Yahoo symbol)."""
    if isinstance(doc, dict) and "chart" in doc:
        sym = name_hint
        if not sym:
            try:
                sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <TICKER>.json")
        sym = _yahoo_of(sym)
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and _is_spark_flat(doc):
        data.update(parse_spark(doc))
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path):
    """A folder of <T>.json(.gz) files or one file -> ({YAHOO: series}, {YAHOO: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and "chart" in doc:
                _absorb(doc, _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- stock lists and the bhavcopy

def read_csv(text):
    """CSV text -> (header, list of dicts keyed by UPPERCASE stripped header; values stripped).
    NSE's files have spaces after the commas."""
    rows, hdr = [], None
    for r in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if not r or not any(c.strip() for c in r):
            continue
        r = [c.strip() for c in r]
        if hdr is None:
            hdr = [h.upper() for h in r]
            continue
        rows.append(dict(zip(hdr, r)))
    if hdr is None:
        raise ScanError("an empty CSV file")
    return hdr, rows


def parse_stock_list(text, eq_only=False):
    """niftyindices constituent list or EQUITY_L.csv -> [{"symbol", "name", "series"}]."""
    hdr, rows = read_csv(text)
    if "SYMBOL" not in hdr or not ({"COMPANY NAME", "NAME OF COMPANY"} & set(hdr)):
        raise ScanError("not a niftyindices constituent list or EQUITY_L.csv (header %s)" % ",".join(hdr)[:60])
    out = []
    for r in rows:
        sym = r.get("SYMBOL", "").upper()
        series = (r.get("SERIES") or "EQ").upper()
        if not sym or (eq_only and series != "EQ"):
            continue
        out.append({"symbol": sym, "name": r.get("COMPANY NAME") or r.get("NAME OF COMPANY") or "",
                    "series": series})
    return out


def _dated(state_dir, prefix):
    """[(date, path)] of state_dir/<prefix>-YYYY-MM-DD.csv, newest first."""
    out = []
    if os.path.isdir(state_dir):
        for f in os.listdir(state_dir):
            m = re.match(r"^%s-(\d{4}-\d{2}-\d{2})\.csv$" % re.escape(prefix), f)
            if m:
                out.append((datetime.date.fromisoformat(m.group(1)), os.path.join(state_dir, f)))
    return sorted(out, reverse=True)


def list_source(kind):
    """'nifty50' / 'all' -> (file prefix, url, eq_only)."""
    if kind == "all":
        return "EQUITY_L", EQUITY_L_URL, True
    if kind not in INDEX_LISTS:
        raise ScanError("--universe is one of %s or all, not %r" % (", ".join(INDEX_LISTS), kind))
    return INDEX_LISTS[kind], LIST_URL % INDEX_LISTS[kind], False


def get_list(kind, state_dir, today, lists_dir=None, refresh=False, fetch=None):
    """-> (rows [{"symbol","name","series"}], note). Offline from lists_dir; otherwise the
    cache in state_dir when younger than LIST_MAX_AGE days, else a download (cached with
    today's date). A failed download falls back to an older cache and says so."""
    prefix, url, eq_only = list_source(kind)
    fname = prefix + ".csv"
    if lists_dir:
        p = os.path.join(lists_dir, fname)
        if not os.path.exists(p):
            raise ScanError("no %s in %s" % (fname, lists_dir))
        with open(p, encoding="utf-8", errors="replace") as fh:
            return parse_stock_list(fh.read(), eq_only), "list %s from %s" % (fname, lists_dir)
    cached = _dated(state_dir, prefix)
    if cached and not refresh and (today - cached[0][0]).days <= LIST_MAX_AGE:
        with open(cached[0][1], encoding="utf-8") as fh:
            return parse_stock_list(fh.read(), eq_only), "list cached %s" % cached[0][0].isoformat()
    try:
        text = (fetch or http_text)(url)
        rows = parse_stock_list(text, eq_only)
        if not rows:
            raise ScanError("the downloaded %s has no rows" % fname)
    except ScanError as e:
        if cached:
            with open(cached[0][1], encoding="utf-8") as fh:
                return (parse_stock_list(fh.read(), eq_only),
                        "list cached %s (a fresh download failed: %s)" % (cached[0][0].isoformat(), e))
        raise
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "%s-%s.csv" % (prefix, today.isoformat())), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write(text.replace("\r", ""))
    return rows, "list downloaded %s" % today.isoformat()


def known_names(state_dir, lists_dir=None):
    """{SYMBOL: (name, series)} from a cached or offline EQUITY_L.csv. Best effort, never the network."""
    path = None
    if lists_dir and os.path.exists(os.path.join(lists_dir, "EQUITY_L.csv")):
        path = os.path.join(lists_dir, "EQUITY_L.csv")
    else:
        c = _dated(state_dir, "EQUITY_L")
        if c:
            path = c[0][1]
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return {r["symbol"]: (r["name"], r["series"]) for r in parse_stock_list(fh.read())}
    except (OSError, ScanError):
        return {}


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_bhav(text):
    """sec_bhavdata_full text -> (date or None, {SYMBOL: row}). One row per symbol, the EQ
    series wins. deliv_qty / deliv_per are None where NSE prints '-'."""
    hdr, rows = read_csv(text)
    if "DELIV_PER" not in hdr or "SYMBOL" not in hdr:
        raise ScanError("not an NSE bhavcopy (sec_bhavdata_full): no DELIV_PER column")
    out, when = {}, None
    for r in rows:
        sym = r.get("SYMBOL", "").upper()
        if not sym:
            continue
        if when is None:
            m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", r.get("DATE1", ""))
            if m and m.group(2).lower() in MONTHS:
                when = datetime.date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
        series = r.get("SERIES", "").upper()
        if sym in out and out[sym]["series"] == "EQ":
            continue
        out[sym] = {"series": series, "prev_close": _num(r.get("PREV_CLOSE")), "close": _num(r.get("CLOSE_PRICE")),
                    "qty": _num(r.get("TTL_TRD_QNTY")), "turnover_lakh": _num(r.get("TURNOVER_LACS")),
                    "trades": _num(r.get("NO_OF_TRADES")), "deliv_qty": _num(r.get("DELIV_QTY")),
                    "deliv_per": _num(r.get("DELIV_PER"))}
    return when, out


def bhav_name(d):
    return "sec_bhavdata_full_%s.csv" % d.strftime("%d%m%Y")


def get_bhav(d, state_dir, lists_dir=None, pause=0.0, fetch=None):
    """The bhavcopy of day d as (date, rows), or None when NSE has no file for that day."""
    name = bhav_name(d)
    if lists_dir:
        p = os.path.join(lists_dir, name)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8", errors="replace") as fh:
            return parse_bhav(fh.read())
    folder = os.path.join(state_dir, "bhav")
    p = os.path.join(folder, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return parse_bhav(fh.read())
    if pause:
        SLEEP(pause)
    try:
        text = (fetch or http_text)(BHAV_URL % d.strftime("%d%m%Y"))
    except NotFound:
        return None
    got = parse_bhav(text)
    os.makedirs(folder, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text.replace("\r", ""))
    files = sorted((f for f in os.listdir(folder) if re.match(r"^sec_bhavdata_full_\d{8}\.csv$", f)),
                   key=lambda f: f[-8:-4] + f[-10:-8] + f[-12:-10])   # oldest first, by YYYYMMDD
    for old in files[:-BHAV_KEEP]:
        try:
            os.remove(os.path.join(folder, old))
        except OSError:
            pass
    return got


def recent_bhavs(now_utc, count, state_dir, lists_dir=None, pause=0.0, fetch=None):
    """The latest `count` bhavcopies, newest first: [(date, rows)]. Walks back over the NSE
    calendar from today (from yesterday while today's session is not over); a trading day
    NSE has no file for is skipped. Returns fewer when the files run out."""
    now = to_ist(now_utc)
    d = now.date()
    s = session(d)
    if s is None or now.time() < s[1]:
        d -= datetime.timedelta(days=1)
    out = []
    for _ in range(BHAV_LOOKBACK):
        if len(out) >= count:
            break
        if trading_day(d)[0]:
            got = get_bhav(d, state_dir, lists_dir, pause, fetch)
            if got:
                out.append(got)
        d -= datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- entries (what to scan)

def make_entry(sym, name="", series=None):
    p = parse_symbol(sym)
    if p["index"]:
        raise IndiaError("%s is an index: scan stocks" % sym)
    if p["tradingsymbol"].isdigit():
        raise IndiaError("%s is a numeric BSE code, which Yahoo does not know: use the ticker (RELIANCE.BO)" % sym)
    return {"symbol": p["tradingsymbol"], "exchange": p["exchange"], "yahoo": p["yahoo"],
            "series": series, "name": name or ""}


def entries_from_symbols(symbols, names=None):
    """['RELIANCE', 'TCS.NS'] -> entries. Series/name come from a cached EQUITY_L when there is one
    (series is null when unknown)."""
    out, seen = [], set()
    for s in symbols:
        try:
            p = parse_symbol(s)
        except IndiaError as e:
            raise ScanError(str(e))
        if p["index"]:
            raise ScanError("%s is an index: scan stocks" % s)
        if p["yahoo"] in seen:
            continue
        seen.add(p["yahoo"])
        name, series = (names or {}).get(p["tradingsymbol"], ("", None))
        out.append(make_entry(s, name, series))
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def read_entries(a, now_utc, offline_yahoos=None):
    names = known_names(a.state_dir, a.lists)
    note = None
    if a.symbols:
        ents = entries_from_symbols([s for s in re.split(r"[,\s]+", a.symbols.upper()) if s], names)
    elif a.watchlist:
        ents = entries_from_symbols(symbols_from_file(a.watchlist), names)
    elif a.universe_file:
        ents = entries_from_symbols(symbols_from_file(a.universe_file), names)
    elif offline_yahoos is not None and not a.universe:
        ents = entries_from_symbols(sorted(offline_yahoos), names)
    else:
        rows, note = get_list(a.universe or DEFAULT_UNIVERSE, a.state_dir, to_ist(now_utc).date(), a.lists,
                              a.refresh_lists)
        ents, seen = [], set()
        for r in rows:
            try:
                e = make_entry(r["symbol"], r["name"], r["series"])
            except IndiaError:
                continue
            if e["yahoo"] not in seen:
                seen.add(e["yahoo"])
                ents.append(e)
    if a.limit:
        ents = ents[:a.limit]
    if not ents:
        raise ScanError("no symbols to scan")
    return ents, note


def tag(e, **extra):
    d = {k: e[k] for k in ("symbol", "exchange", "yahoo", "series", "name")}
    d.update(extra)
    return d


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes, anything dated after the clock dropped."""
    now = to_ist(now_utc)
    today = now.date()
    s = session(today)
    today_done = s is None or now.time() >= s[1]
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = ts_to_ist(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = float(c)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, entries, now_utc, min_price, min_turnover=0.0, liquidity=None):
    """Pure. data = {YAHOO: series}; liquidity = {SYMBOL: bhavcopy row} or None.
    Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(entries), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for e in entries:
        s = data.get(e["yahoo"])
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_closes(s, now_utc)
        price = bars[-1][1] if bars else None
        liq = (liquidity or {}).get(e["symbol"])
        turn = liq["turnover_lakh"] if liq else None
        if price is None or price < min_price or (min_turnover > 0 and (turn is None or turn < min_turnover)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append(tag(e, setups=tags, price=round(closes[-1], 2), close_date=d_last.isoformat(),
                            rsi=round(nums["rsi"], 1),
                            volume=int(liq["qty"]) if liq and liq["qty"] is not None else None,
                            turnover_lakh=round(turn, 2) if turn is not None else None))
    hits.sort(key=lambda h: (-(h["turnover_lakh"] or 0), h["symbol"]))  # most rupees traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- delivery

def scan_delivery(entries, latest, history, per_rise=10.0, qty_mult=2.0, min_deliv_per=40.0, min_price=0.0,
                  min_turnover=0.0, min_history=3):
    """Pure. latest = {SYMBOL: bhav row}, history = [{SYMBOL: row}, ...] the earlier days.
    A stock is listed when its delivery % is at least its own average + per_rise points and at
    least min_deliv_per, and its delivery quantity is at least qty_mult times its own average.
    Returns (hits, counts)."""
    hits = []
    counts = {"requested": len(entries), "in_bhavcopy": 0, "no_delivery_data": 0, "illiquid": 0,
              "short": 0, "scanned": 0}
    for e in entries:
        r = latest.get(e["symbol"])
        if not r or r["series"] != "EQ":
            continue
        counts["in_bhavcopy"] += 1
        if r["deliv_per"] is None or r["deliv_qty"] is None or not r["close"]:
            counts["no_delivery_data"] += 1
            continue
        if r["close"] < min_price or (min_turnover > 0 and (r["turnover_lakh"] or 0) < min_turnover):
            counts["illiquid"] += 1
            continue
        pers, qtys = [], []
        for day in history:
            h = day.get(e["symbol"])
            if h and h["series"] == "EQ" and h["deliv_per"] is not None and h["deliv_qty"] is not None:
                pers.append(h["deliv_per"])
                qtys.append(h["deliv_qty"])
        if len(pers) < min_history:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        avg_per = sum(pers) / len(pers)
        avg_qty = sum(qtys) / len(qtys)
        if avg_qty <= 0:
            continue
        x = r["deliv_qty"] / avg_qty
        if r["deliv_per"] >= avg_per + per_rise and r["deliv_per"] >= min_deliv_per and x >= qty_mult:
            chg = (r["close"] / r["prev_close"] - 1) * 100 if r["prev_close"] else None
            hits.append(tag(e, setups=["delivery"], price=round(r["close"], 2),
                            chg_pct=None if chg is None else round(chg, 2),
                            deliv_per=round(r["deliv_per"], 2), avg_deliv_per=round(avg_per, 2),
                            deliv_qty=int(r["deliv_qty"]), avg_deliv_qty=int(round(avg_qty)),
                            qty_x=round(x, 2), turnover_lakh=r["turnover_lakh"],
                            deliv_value_lakh=round(r["deliv_qty"] * r["close"] / 1e5, 2),
                            history_days=len(pers)))
    hits.sort(key=lambda h: (-h["deliv_value_lakh"], h["symbol"]))
    counts["hits"] = len(hits)
    return hits, counts


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    s = session(day)
    return datetime.datetime.combine(day, s[0] if s else OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff, close_t=None):
    """5-minute bars of `day` that are complete at `cutoff` (naive India time),
    on the grid, with a close. Sorted, one per slot. close_t: where this stock's
    continuous trading ends (continuous_close; default the session close)."""
    s = session(day)
    open_t, end_t = s if s else (OPEN, CLOSE)
    close_t = close_t or end_t
    out = {}
    hs, ls = series.get("h"), series.get("l")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        it = ts_to_ist(ts)
        if it.date() != day or it.second or it.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if it.time() < open_t or it.time() >= close_t:
            continue  # pre-open, after hours, the closing print
        if it + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        v = series.get("v")
        out[it] = {"start": it, "c": float(c),
                   "h": None if not hs or hs[i] is None else float(hs[i]),
                   "l": None if not ls or ls[i] is None else float(ls[i]),
                   "v": (v[i] or 0) if v else 0}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range, close_t=None):
    """Every slot from the open to the cutoff is there, and at least the range.
    close_t: where this stock's continuous trading ends (default the session close)."""
    want = int((cutoff - slot(day, 0)).total_seconds() // (BAR_MIN * 60))
    s = session(day)
    if s:
        want = min(want, int((datetime.datetime.combine(day, close_t or s[1]) - slot(day, 0)).total_seconds()
                             // (BAR_MIN * 60)))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k) for k, b in enumerate(bars))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": clock_label(closes_at),
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    now = to_ist(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = trading_day(day)
    if not ok:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    s = session(day)
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, s[1])
    if day.isoformat() in SPECIAL_SESSIONS:   # special session (Muhurat): entries stop 15 min before the close
        until = (close_dt - datetime.timedelta(minutes=15)).time()
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s IST on %s. Replay the session with --at 10:15."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s IST: the %d-minute opening range runs %s to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), or_minutes, clock_label(slot(day, 0)), clock_label(range_end),
                           clock_label(first_close), clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                      clock_label(first_close)))
    return day, cutoff


def stock_close(day, e, cas_stocks):
    """Where continuous trading ends for entry e: cas_stocks is None (no closing-auction info: the
    session close), "all" (could not tell which stocks: assume all F&O-like, the cautious end) or a
    set of F&O trading symbols."""
    if cas_stocks is None:
        return None
    return continuous_close(day, cas_stocks == "all" or e["symbol"].upper() in cas_stocks)


def scan_orb(stage1, entries, day, cutoff, or_minutes, min_price=0.0, min_turnover=0.0, liquidity=None,
             confirm=None, max_confirm=None, cas_stocks=None):
    """stage1 = {YAHOO: series} with closes (spark or v8). confirm(entry) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (turnover of the last bhavcopy, then the strongest close) are confirmed;
    the rest are counted as unconfirmed. cas_stocks: the F&O stocks, whose session ends at
    continuous_close (15:15 while the closing auction rule is in force), see stock_close.
    Returns (hits, counts, failed {yahoo: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(entries), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "gappy": 0, "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for e in entries:
        s = stage1.get(e["yahoo"])
        if not s:
            continue
        counts["fetched"] += 1
        end = stock_close(day, e, cas_stocks)
        bars = session_bars(s, day, cutoff, end)
        complete = usable(bars, day, cutoff, n_range, end)
        # Yahoo's spark leaves out some 5-minute bars that the v8 chart has (a third of the Nifty 50 by
        # 14:00 on 2026-10-23). With a stage 2 to decide, a gappy chart is not thrown away: it goes on.
        if not bars or (not complete and confirm is None):
            counts["unusable"] += 1
            continue
        liq = (liquidity or {}).get(e["symbol"])
        turn = liq["turnover_lakh"] if liq else None
        if bars[-1]["c"] < min_price or (min_turnover > 0 and (turn is None or turn < min_turnover)):
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if not complete:
            counts["gappy"] += 1
            cands.append((e, bars, (turn or 0.0, 0.0)))
        elif orb_candidate(bars, n_range):
            top = max(b["c"] for b in bars[:n_range])
            cands.append((e, bars, (turn or 0.0, bars[-1]["c"] / top)))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: (-x[2][0], -x[2][1], x[0]["symbol"]))
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for e, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(e)
            except ScanError as err:
                failed[e["yahoo"]] = str(err)
                continue
            full = session_bars(s2, day, cutoff, stock_close(day, e, cas_stocks))
            if not usable(full, day, cutoff, n_range, stock_close(day, e, cas_stocks)):
                failed[e["yahoo"]] = "the v8 chart has a missing bar"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            hits.append(tag(e, setups=["orb"], **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(entries, rng, interval, pause, a=None):
    """Batches of 20. -> ({YAHOO: series}, {YAHOO: error})."""
    data, failed = {}, {}
    ys = [e["yahoo"] for e in entries]
    batches = [ys[i:i + SPARK_MAX] for i in range(0, len(ys), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)))
        except (ScanError, ValueError) as e:
            for y in batch:
                failed[y] = str(e)
            continue
        for y in batch:
            if y in got:
                data[y] = got[y]
            else:
                failed[y] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def n(x):
    return "{:,}".format(x)


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def _p(x):
    return "{:,.2f}".format(x)


def load_liquidity(a, now, needed):
    """Latest bhavcopy rows for turnover/volume: (date, rows) or None. Best effort unless `needed`
    (a --min-turnover-lakh floor was asked for), then a failure is an error."""
    if a.lists is None and getattr(a, "offline", None) and not needed:
        return None     # an offline chart replay never touches the network for ranking
    try:
        got = recent_bhavs(now, 1, a.state_dir, a.lists, a.pause)
    except ScanError as e:
        if needed:
            raise ScanError("--min-turnover-lakh needs the NSE bhavcopy: %s" % e)
        return None
    if not got:
        if needed:
            raise ScanError("--min-turnover-lakh needs the NSE bhavcopy and none could be found")
        return None
    return got[0]


def big_inr(x):
    """12.30 crore with the rupee sign."""
    s = lakh_crore(x)
    return s if s.startswith(("-", RUPEE)) else RUPEE + s


def calendar_warning(day):
    note = calendar_note(day)
    if note:
        print("note: " + note, file=sys.stderr)


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    now = parse_now_scan(a.now)
    kind = a.universe or DEFAULT_UNIVERSE
    rows, note = get_list(kind, a.state_dir, to_ist(now).date(), a.lists, a.refresh_lists)
    series = {}
    for r in rows:
        series[r["series"]] = series.get(r["series"], 0) + 1
    if a.out:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write("%s|%s\n" % (r["symbol"], r["name"]))
    if a.json:
        print(json.dumps({"universe": kind, "count": len(rows), "series": series, "source": note,
                          "out": a.out}, indent=2))
    else:
        print("%s: %s stocks (%s)." % (kind, n(len(rows)), note))
        print("Series: " + ", ".join("%s %s" % (k, n(v)) for k, v in sorted(series.items())) + ".")
        if a.out:
            print("Wrote %s (one SYMBOL|Name a line)." % a.out)
    return 0 if rows else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now_scan(a.now)
    calendar_warning(to_ist(now).date())
    offline_ys = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_ys = sorted(set(data) | set(failed))
    entries, note = read_entries(a, now, offline_ys)
    liq = load_liquidity(a, now, a.min_turnover_lakh > 0)
    liquidity = liq[1] if liq else None
    if not a.offline:
        progress(a, "Fetching daily closes for %s symbols (%d spark requests)..."
                 % (n(len(entries)), (len(entries) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(entries, "3mo", "1d", a.pause, a)
    hits, counts, last_date = scan_daily(data, entries, now, a.min_price, a.min_turnover_lakh, liquidity)
    nfailed = len(entries) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "nse-scanner", "created_at": iso(now), "mode": "daily",
           "as_of": as_of, "universe": len(entries), "scanned": counts["scanned"], "failed": nfailed,
           "source": "yahoo", "filters": {"min_price": a.min_price, "min_turnover_lakh": a.min_turnover_lakh},
           "note": NOTE, "hits": hits}
    if liq:
        doc["liquidity_from"] = "bhavcopy %s" % liq[0].isoformat()
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/\\"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2,
                         ensure_ascii=False))
        return 0 if hits else 1
    print("Daily scan, %s IST, closes through %s (Yahoo spark, daily closes only)." % (
        to_ist(now).strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (close %s+%s)%s; %s scanned." % (
        n(counts["fetched"]), n(len(entries)),
        (" (%s failed or had no data)" % n(nfailed)) if nfailed else "",
        n(counts["fetched"] - counts["illiquid"]), inr(a.min_price, 0 if a.min_price == int(a.min_price) else 2),
        (" and %s lakh traded" % _n(a.min_turnover_lakh)) if a.min_turnover_lakh > 0 else "",
        (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
        n(counts["scanned"])))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (c, LABEL[k]) for k, c in per if c)))
        print("Most traded first (turnover of the bhavcopy of %s):" % liq[0].isoformat() if liq
              else "Listed A to Z (no bhavcopy turnover to rank by):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-12s %12s  RSI %5.1f  %s" % (h["symbol"], inr(h["price"]), h["rsi"],
                                              ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /nse-news TICKER.")
    return 0 if hits else 1


def cmd_orb(a):
    t0 = time.time()
    now = parse_now_scan(a.now)
    at = parse_hhmm_flag(a.at) if a.at else None
    cfg = read_config(a.config)
    until = parse_hhmm_flag(a.until or cfg.get("no_entries_after") or "14:45", "--until")
    data = failed = None
    explicit_day = None
    offline_ys = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_ys = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_ist(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day)
    calendar_warning(day)
    cas_stocks, cas_note, auction = None, None, None
    if continuous_close(day, True) != continuous_close(day, False):
        found, cas_note = fo_stocks(to_ist(utc_now()).date(), offline=bool(a.offline), fo_file=a.fo_lots,
                                    state_dir=a.state_dir)
        cas_stocks = found if found is not None else "all"
        auction = ("Closing auction: F&O stocks stop continuous trading at %s, not %s (no stop-loss orders in "
                   "the auction); their bars are read to then." % (clock_label(datetime.datetime.combine(
                       day, continuous_close(day, True))), clock_label(datetime.datetime.combine(
                           day, continuous_close(day, False)))))
    entries, note = read_entries(a, now, offline_ys)
    liq = load_liquidity(a, now, a.min_turnover_lakh > 0)
    liquidity = liq[1] if liq else None
    confirm = None
    if not a.offline:
        today = to_ist(utc_now()).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % (n(len(entries)), (len(entries) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(entries, rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(e):
            if a.pause:
                SLEEP(a.pause)
            done.append(e["yahoo"])
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_path(e["yahoo"], rng, "%dm" % BAR_MIN)), e["yahoo"])
    hits, counts, failed2 = scan_orb(data, entries, day, cutoff, a.or_minutes, a.min_price, a.min_turnover_lakh,
                                     liquidity, confirm, a.max_confirm, cas_stocks)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([e for e in entries if e["yahoo"] in failed])
    as_of = "%s %s IST" % (day.isoformat(), cutoff.strftime("%H:%M"))
    doc = {"created_by": "nse-scanner", "created_at": iso(now), "mode": "orb",
           "as_of": as_of, "universe": len(entries), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"], "source": "yahoo", "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_turnover_lakh": a.min_turnover_lakh}, "note": NOTE,
           "hits": hits}
    if auction:
        doc["closing_auction"] = auction
        if cas_note:
            doc["fo_list_note"] = cas_note
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/\\"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2,
                         ensure_ascii=False))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (%s to %s), 5-minute bars, Yahoo."
          % (day_label(day), clock_label(cutoff), a.or_minutes, clock_label(slot(day, 0)),
             clock_label(slot(day, a.or_minutes // BAR_MIN))))
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % (n(counts["fetched"]), n(len(entries)),
             (" (%s failed or had no data)" % n(nfailed)) if nfailed else "",
             n(counts["scanned"]),
             (" (%s under the price / turnover floor)" % n(counts["illiquid"])) if counts["illiquid"] else "",
             n(counts["candidates"]), n(counts["confirmed"])))
    if auction:
        print(auction)
        if cas_note:
            print(cas_note + ".")
    if counts["gappy"]:
        print("%s of the candidates have 5-minute bars missing in Yahoo's spark answer, so the full chart "
              "decides them." % n(counts["gappy"]))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % (n(counts["unconfirmed"]), n(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), ("the %s checked" % n(counts["confirmed"])) if counts["unconfirmed"]
                 else n(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %8s  %-12s %12s  range %s to %s  %+.2f%% over" % (
                h["at"], h["symbol"], inr(h["price"]), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /nse-news TICKER and /nse-entry TICKER.")
    return 0 if hits else 1


def cmd_delivery(a):
    t0 = time.time()
    now = parse_now_scan(a.now)
    calendar_warning(to_ist(now).date())
    if not 1 <= a.history <= 30:
        raise ScanError("--history wants 1 to 30 earlier bhavcopies (5 to 10 is typical)")
    entries, note = read_entries(a, now)
    progress(a, "Reading the NSE bhavcopies (latest + up to %d earlier trading days)..." % a.history)
    days = recent_bhavs(now, a.history + 1, a.state_dir, a.lists, a.pause)
    if not days:
        raise ScanError("no NSE bhavcopy found in the last %d days (archives.nseindia.com): try again after "
                        "6 PM IST, or check your connection" % BHAV_LOOKBACK)
    latest_date, latest = days[0]
    hist = [rows for _, rows in days[1:]]
    if len(hist) < a.min_history:
        raise ScanError("only %d earlier bhavcopies found, %d needed (--min-history): not enough to average"
                        % (len(hist), a.min_history))
    hits, counts = scan_delivery(entries, latest, hist, a.per_rise, a.qty_mult, a.min_delivery_pct, a.min_price,
                                 a.min_turnover_lakh, a.min_history)
    doc = {"created_by": "nse-scanner", "created_at": iso(now), "mode": "delivery",
           "as_of": "%s close" % latest_date.isoformat(), "universe": len(entries), "scanned": counts["scanned"],
           "failed": len(entries) - counts["in_bhavcopy"], "source": "nse bhavcopy",
           "history": [d.isoformat() for d, _ in days[1:]],
           "filters": {"per_rise": a.per_rise, "qty_mult": a.qty_mult, "min_delivery_pct": a.min_delivery_pct,
                       "min_history": a.min_history, "min_price": a.min_price,
                       "min_turnover_lakh": a.min_turnover_lakh},
           "note": NOTE + " " + DELIVERY_NOTE, "hits": hits}
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2,
                         ensure_ascii=False))
        return 0 if hits else 1
    print("Delivery scan, bhavcopy of %s, against the average of the %d earlier trading days (%s to %s)." % (
        day_label(latest_date), len(hist), days[-1][0].isoformat(), days[1][0].isoformat()))
    print("%s of %s stocks are in the bhavcopy (EQ series)%s; %s scanned (%d+ earlier days of delivery data)%s." % (
        n(counts["in_bhavcopy"]), n(len(entries)),
        (", %d without delivery data" % counts["no_delivery_data"]) if counts["no_delivery_data"] else "",
        n(counts["scanned"]), a.min_history,
        (", %d under the price / turnover floor" % counts["illiquid"]) if counts["illiquid"] else ""))
    if hits:
        print("%d stock%s with delivery well above its own average (delivery %% at least +%s points and "
              "%s%%, quantity at least %sx):" % (len(hits), "" if len(hits) == 1 else "s", _n(a.per_rise),
                                                  _n(a.min_delivery_pct), _n(a.qty_mult)))
        print("  %-12s %12s %8s  %-17s %-22s %s" % ("", "close", "change", "delivery %", "delivery qty", "value"))
        for h in hits[:a.show]:
            chg = "%+.2f%%" % h["chg_pct"] if h["chg_pct"] is not None else "-"
            print("  %-12s %12s %8s  %5.1f (avg %5.1f)  %11s (%4.1fx avg)  %s" % (
                h["symbol"], inr(h["price"]), chg, h["deliv_per"], h["avg_deliv_per"], n(h["deliv_qty"]),
                h["qty_x"], big_inr(h["deliv_value_lakh"] * 1e5)))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No stock stood out.")
    if not a.no_write:
        print("Wrote %s (mode delivery, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " " + DELIVERY_NOTE + " Next: pick one, then /nse-news TICKER.")
    return 0 if hits else 1


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Chart Scanner (nse-scanner): daily setups, opening-range "
                                             "breakouts and delivery spikes on NSE stocks.")
    sub = ap.add_subparsers(dest="cmd")

    def lists_opts(p):
        p.add_argument("--universe", help="%s or all (default %s)" % (", ".join(INDEX_LISTS), DEFAULT_UNIVERSE))
        p.add_argument("--lists", metavar="DIR", help="offline: a folder with the Nifty list CSVs, "
                                                      "EQUITY_L.csv and sec_bhavdata_full_*.csv")
        p.add_argument("--refresh-lists", action="store_true", help="download the stock list again")
        p.add_argument("--state-dir", default=STATE_DIR)
        p.add_argument("--now", help='pretend it is this time ("2026-10-23 10:15" India time, or ...Z)')
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("universe", help="refresh the cached stock list")
    lists_opts(p)
    p.add_argument("--out", help="also write one SYMBOL|Name a line to this file")

    def common(p, min_price):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated tickers instead of the universe (RELIANCE,TCS.NS,M&M)")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of tickers")
        g.add_argument("--universe-file", metavar="FILE", help="a text file, one SYMBOL|Name a line")
        lists_opts(p)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price,
                       help="last close at least this many rupees (%s)" % _n(min_price))
        p.add_argument("--min-turnover-lakh", type=float, default=0.0,
                       help="turnover of the last bhavcopy at least this many lakh (default 0 = off)")
        p.add_argument("--out", default=WATCHLIST)
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("daily", help="setups from daily closes")
    common(p, 10.0)
    p.add_argument("--offline", metavar="PATH", help="chart fixture folder or file instead of the network")
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 10.0)
    p.add_argument("--offline", metavar="PATH", help="chart fixture folder or file instead of the network")
    p.add_argument("--fo-lots", metavar="PATH", help="fo_mktlots.csv (which stocks end at 15:15 in the closing "
                   "auction); default: NSE's file cached in nse/state, or the test fixture with --offline")
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this India time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, 14:45)")
    p.add_argument("--config", default=CONFIG)
    p = sub.add_parser("delivery", help="delivery %% and quantity spikes from the NSE bhavcopy")
    common(p, 0.0)
    p.add_argument("--history", type=int, default=7, help="earlier bhavcopies to average (7)")
    p.add_argument("--per-rise", type=float, default=10.0,
                   help="delivery %% points above the stock's own average (10)")
    p.add_argument("--qty-mult", type=float, default=2.0, help="delivery quantity times its own average (2.0)")
    p.add_argument("--min-delivery-pct", type=float, default=40.0, help="and at least this delivery %% (40)")
    p.add_argument("--min-history", type=int, default=3, help="earlier days a stock needs (3)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        load_calendar(HOLIDAYS)
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb,
                "delivery": cmd_delivery}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
