#!/usr/bin/env python3
"""News monitor for nse-news. Standard library only.

Watches headlines for the NSE/BSE stocks you watch or hold and prints only
what is new since the last check, newest first, with how far each stock has
moved today and a flag on anything that touches your tickers.

    python3 news.py                                  # watchlist + plan + portfolio tickers
    python3 news.py --tickers RELIANCE TCS "M&M"
    python3 news.py --tickers TCS --all --hours 72   # everything in the last 3 days, seen or not
    python3 news.py --no-moves                       # skip the price line per ticker

Your tickers, when --tickers is not given: every hit in nse/watchlist.json
(nse-scanner), the symbol in nse/plan.json (nse-exit) and the positions in
nse/portfolio.json.

Sources, one request each per ticker:
  - Google News RSS (English, India edition), searching the company's name
    (from NSE's EQUITY_L.csv, cached in nse/state/) and the ticker:
    https://news.google.com/rss/search?q="Reliance Industries" OR "RELIANCE share" when:48h&hl=en-IN&gl=IN&ceid=IN:en
    Yahoo's RSS feed is empty for Indian stocks, so it is not used.
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against the previous close:
    https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m

NOT covered: NSE's corporate announcements (results dates, board meetings,
insider and SEBI disclosures). They live behind nseindia.com's bot-protected
API, which this tool does not scrape. Check nseindia.com > Corporate
Announcements by hand for the tickers that matter.

Times print in India time (IST). State: nse/state/news.json remembers what
you have seen (30 days), so an item shows once. Offline: --offline DIR reads
gnews-<TICKER>.xml, chart-<TICKER>.json and EQUITY_L.csv from DIR (a
character that is not a letter, digit, - or _ becomes _, so M&M is M_M). The
tests use it.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import csv
import email.utils
import gzip
import hashlib
import html
import io
import xml.etree.ElementTree as ET


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


# ================================================================ the news monitor

NewsError = IndiaError

GNEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
EQUITY_L_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
STATE = "nse/state/news.json"
KEEP_DAYS = 30
LIST_MAX_AGE = 7   # days a cached EQUITY_L.csv is reused
MOVE_PCT = 3.0     # a stock this far from the previous close is flagged as moving
PER_TICKER = 20    # newest headlines kept per ticker
NOT_COVERED = ("Not covered: NSE corporate announcements (results dates, board meetings, SEBI and insider "
               "disclosures) sit behind nseindia.com's bot-protected API and are not fetched. Check "
               "nseindia.com > Corporate Announcements by hand.")

KEYWORDS = [
    ("results", r"\bQ[1-4]\b|\bresults?\b|\bearnings\b|\bnet profit\b|\bPAT\b|\bEBITDA\b|\bguidance\b"),
    ("dividend", r"\bdividends?\b|\brecord date\b|\bex-date\b|\bpayout\b"),
    ("bonus", r"\bbonus\b"),
    ("split", r"\b(?:stock|share) split\b|\bsub-?division\b|\bface value\b|\bsplit\b"),
    ("buyback", r"\bbuy-?back\b|\brepurchase\b|\btender offer\b"),
    ("order win", r"\border win\b|\borders? (?:worth|inflow|book)\b|\bletter of (?:award|intent)\b"
                  r"|\b(?:bags?|wins?|secures?|lands?|receives?)\b[^.]{0,40}\b(?:orders?|contracts?)\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)s?\b|\bmerger\b|\bdemerger\b|\btakeover\b|\bstake (?:sale|buy)\b"
             r"|\b(?:buys?|sells?|picks? up) (?:a )?stake\b|\bjoint venture\b|\bdeal\b"),
    ("block/bulk deal", r"\b(?:block|bulk) deals?\b"),
    ("rating/target", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|\btarget price\b|\bprice target\b|\bbrokerage\b"
                      r"|\boutperform\b|\bunderperform\b|\boverweight\b|\bunderweight\b|\b(?:buy|sell) rating\b"
                      r"|\b(?:sets?|raises?|cuts?|hikes?|lowers?)\b[^.]{0,30}\btargets?\b|\btargets? of\b"),
    ("SEBI/regulatory", r"\bSEBI\b|\bshow[- ]cause\b|\bpenalty\b|\bfined?\b|\bCCI\b|\bNCLT\b|\bprobe\b"
                        r"|\binvestigation\b|\binsider trading\b|\bforensic audit\b"),
    # a change, not any sentence with "CEO" in it (a CEO's views on AI are not a management change)
    ("management change", r"\bresign|\bsteps? down\b|\bstepping down\b|\bappoint|\bsuccession\b|\bquits?\b"
                          r"|\b(?:new|interim|incoming|outgoing)\s+(?:CEO|CFO|MD|managing director|chairman)\b"
                          r"|\b(?:names?|named|elevates?|elevated)\b[^.]{0,40}\b(?:CEO|CFO|MD|chairman)\b"),
    ("fund raise", r"\bQIP\b|\bIPO\b|\bFPO\b|\bOFS\b|\boffer for sale\b|\brights issue\b"
                   r"|\bpreferential (?:issue|allotment)\b|\bfund-?rais|\braises? (?:Rs|₹)|\bNCDs?\b|\bwarrants\b"),
    ("macro/RBI", r"\bRBI\b|\brepo rate\b|\bMPC\b|\binflation\b|\bCPI\b|\bGDP\b|\bFed\b|\bFOMC\b|\bcrude\b"
                  r"|\bGST\b|\bBudget\b|\bFIIs?\b|\bFPIs?\b|\bDIIs?\b|\btariffs?\b|\brupee\b"),
    ("circuit", r"\b(?:upper|lower) circuit\b|\bASM\b|\bGSM\b|\btrading halt|\bsuspended\b|\bdelist"),
]


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def norm(text):
    """RELIANCE, reliance.ns, NSE:TCS -> 'RELIANCE', 'TCS'; a BSE stock keeps '.BO'. Indices raise."""
    p = parse_symbol(text)
    if p["index"]:
        raise NewsError("%s is an index: news is per stock" % text)
    return p["tradingsymbol"] if p["exchange"] == "NSE" else p["yahoo"]


def safe(t):
    return re.sub(r"[^A-Za-z0-9_-]", "_", t)


def watch_from_files(portfolio="nse/portfolio.json", watchlist="nse/watchlist.json", plan="nse/plan.json"):
    """{ticker: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}

    def add(sym, name, override=False):
        if not sym:
            return
        try:
            k = norm(sym)
        except IndiaError:
            return
        if override or k not in out or (name and not out[k]):
            out[k] = name or out.get(k)

    for h in (load_json(watchlist) or {}).get("hits", []):
        add(h.get("symbol") or h.get("ticker"), h.get("name"))
    pl = load_json(plan) or {}
    add(pl.get("symbol"), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in (pf.get("positions") or []) + (pf.get("holdings") or []):
        add(p.get("symbol") or p.get("ticker") or p.get("tradingsymbol"), p.get("name"))
    return out


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.5):
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def _file(self, name):
        path = os.path.join(self.offline, name)
        if not os.path.isfile(path):
            raise NewsError("offline: no file %s" % path)
        with open(path, "rb") as fh:
            return fh.read()

    def _wait(self):
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()

    def get(self, url, name):
        """A Google News feed or an archives.nseindia.com file."""
        if self.offline:
            return self._file(name)
        self._wait()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip",
                                                   "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url.split("?")[0], exc.reason))
        except OSError as exc:
            raise NewsError("network error for %s: %s" % (url.split("?")[0], exc))

    def chart(self, ysym, name):
        if self.offline:
            return self._file(name)
        self._wait()
        return yahoo_get(chart_path(ysym, "1d", "5m")).encode("utf-8")


# ---------------------------------------------------------------- company names

def clean_name(name):
    """'Reliance Industries Limited' -> 'Reliance Industries'."""
    n = re.sub(r"\s+", " ", name or "").strip()
    n = re.sub(r"\s*\(.*?\)\s*$", "", n)
    n = re.sub(r"[\s,]+(?:limited|ltd\.?)$", "", n, flags=re.I)
    return n.strip()


def parse_equity_l(data):
    """EQUITY_L.csv -> {SYMBOL: company name}. The header has spaces after the commas."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    hdr, out = None, {}
    for r in csv.reader(io.StringIO(text.lstrip("﻿"))):
        r = [c.strip() for c in r]
        if not any(r):
            continue
        if hdr is None:
            hdr = [h.upper() for h in r]
            if "SYMBOL" not in hdr or "NAME OF COMPANY" not in hdr:
                raise NewsError("not NSE's EQUITY_L.csv (header %s)" % ",".join(hdr)[:60])
            continue
        row = dict(zip(hdr, r))
        if row.get("SYMBOL"):
            out[row["SYMBOL"].upper()] = row["NAME OF COMPANY"]
    return out


def equity_l(fetcher, state_dir, today):
    """{SYMBOL: name}. Offline: DIR/EQUITY_L.csv. Online: nse/state/EQUITY_L-YYYY-MM-DD.csv when
    younger than LIST_MAX_AGE days, else a download (an older cache is used if it fails)."""
    if fetcher.offline:
        return parse_equity_l(fetcher.get(EQUITY_L_URL, "EQUITY_L.csv"))
    cached = []
    if os.path.isdir(state_dir):
        for f in os.listdir(state_dir):
            m = re.match(r"^EQUITY_L-(\d{4}-\d{2}-\d{2})\.csv$", f)
            if m:
                cached.append((datetime.date.fromisoformat(m.group(1)), os.path.join(state_dir, f)))
    cached.sort(reverse=True)
    if cached and (today - cached[0][0]).days <= LIST_MAX_AGE:
        with open(cached[0][1], "rb") as fh:
            return parse_equity_l(fh.read())
    try:
        data = fetcher.get(EQUITY_L_URL, "EQUITY_L.csv")
        names = parse_equity_l(data)
    except NewsError:
        if cached:
            with open(cached[0][1], "rb") as fh:
                return parse_equity_l(fh.read())
        raise
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "EQUITY_L-%s.csv" % today.isoformat()), "wb") as fh:
        fh.write(data.replace(b"\r", b""))
    return names


def fill_names(fetcher, watch, state_dir, today, errors):
    """Fill in the company name of every ticker that has none."""
    if all(watch.values()):
        return
    try:
        names = equity_l(fetcher, state_dir, today)
    except NewsError as exc:
        errors.append("company names (EQUITY_L.csv): %s; searching by ticker only" % exc)
        return
    for t in watch:
        if not watch[t]:
            watch[t] = names.get(t.split(".")[0]) or None


def build_query(ticker, name, hours):
    """The Google News query: the company name or the ticker, within the window."""
    sym = ticker.split(".")[0]
    if name:
        q = '"%s" OR "%s share"' % (clean_name(name), sym)
    else:
        q = '"%s share" OR "%s NSE"' % (sym, sym)
    h = max(1, int(round(hours)))
    return "%s when:%s" % (q, ("%dh" % h) if h < 24 * 7 else "7d")


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.astimezone(datetime.timezone.utc)


def parse_gnews(data, ticker):
    """Google News RSS -> items. The title ends with ' - Source'; the source is also its own element."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = html.unescape(_text(it, "title"))
        source = html.unescape(_text(it, "source")) or "Google News"
        suffix = " - " + source
        if source != "Google News" and title.endswith(suffix):
            title = title[:-len(suffix)].rstrip()
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "gn:" + guid, "source": source, "kind": "headline", "ticker": ticker,
                    "title": title, "summary": "", "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": datetime.datetime.fromtimestamp(when, datetime.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        try:
            data = fetcher.chart(parse_symbol(t)["yahoo"], "chart-%s.json" % safe(t))
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    """{ticker: (ticker regex, company-name regex or None)}. Tickers match as written (upper case);
    the company name matches in any case."""
    pats = {}
    for t, name in watch.items():
        sym = t.split(".")[0]
        alts = [r"\(%s\)" % re.escape(sym), r"\b(?:NSE|BSE)\s*:\s*%s\b" % re.escape(sym)]
        if len(sym) >= 5:
            # long tickers in any case: the press writes HCLTech, Infosys's INFY stays INFY
            alts.append(r"(?i:(?<![A-Za-z0-9&])%s(?![A-Za-z0-9&]))" % re.escape(sym))
        elif len(sym) >= 3:
            alts.append(r"(?<![A-Za-z0-9&])%s(?![A-Za-z0-9&])" % re.escape(sym))
        nm = clean_name(name)
        rx_name = None
        if nm and len(nm) >= 4:
            rx_name = re.compile(r"\b%s\b" % re.escape(nm).replace(r"\ ", r"[\s-]"), re.I)
        pats[t] = (re.compile("|".join(alts)), rx_name)
    return pats


def tag(items, watch):
    """Which of your tickers each item touches, and what kind of news it is."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        mentioned = set()
        for t, (rx_t, rx_n) in pats.items():
            if rx_t.search(hay) or (rx_n and rx_n.search(hay)):
                mentioned.add(t)
        touches = set(mentioned)
        if it["ticker"] in watch:
            touches.add(it["ticker"])
        it["touches"] = sorted(touches)
        # Google News matches loosely: a story the search returned for TCS that never names TCS
        # (an Infosys story, a market wrap) is "loose" and hidden unless --loose.
        it["mentioned"] = sorted(mentioned)
        it["loose"] = it["ticker"] in watch and it["ticker"] not in mentioned
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for key in ("touches", "mentioned"):
                for t in it.get(key, []):
                    if t not in hit.setdefault(key, []):
                        hit[key].append(t)
                hit.get(key, []).sort()
            hit["loose"] = hit.get("loose", False) and it.get("loose", False)
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, hours=48, per_ticker=PER_TICKER, errors=None):
    errors = [] if errors is None else errors
    items = []
    for t in sorted(watch):
        url = GNEWS % urllib.parse.quote_plus(build_query(t, watch[t], hours))
        try:
            got = parse_gnews(fetcher.get(url, "gnews-%s.xml" % safe(t)), t)
        except NewsError as exc:
            errors.append("headlines for %s: %s" % (t, exc))
            continue
        got.sort(key=lambda i: i["time"] or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
                 reverse=True)
        items += got[:per_ticker] if per_ticker else got
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - datetime.timedelta(hours=hours)
    ahead = now + datetime.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def ist_text(dt, fmt):
    return to_ist(dt).strftime(fmt)


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % ist_text(m["time"], "%H:%M")) if m.get("time") else ""
        out.append("%-12s %s %+.2f%% vs previous close %s%s  %s" % (
            m["ticker"], inr(m["price"]), m["pct"], inr(m["prev_close"]), when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT, links=False, hidden_loose=0):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = ist_text(it["time"], "%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("  %s  %-11s %-20s %s" % (when, who, it["source"][:20], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        if links and it["link"]:
            extra.append(it["link"])
        if extra:
            out.append("      " + "  ".join(extra))
    if hidden_loose:
        out.append("(%d loosely matched headline%s hidden: the search returned them but they never name the "
                   "stock. --loose shows them.)" % (hidden_loose, "" if hidden_loose == 1 else "s"))
    if items and not links:
        out.append("Links: --links prints them (long Google News redirects), or search the headline.")
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % ist_text(now, "%Y-%m-%d %H:%M IST"))
    out.append(NOT_COVERED)
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("symbols", nargs="*", metavar="TICKER", help="e.g. news.py TCS HCLTECH (same as --tickers)")
    ap.add_argument("--tickers", nargs="*", help="default: watchlist + plan + portfolio")
    ap.add_argument("--links", action="store_true", help="print each story's link")
    ap.add_argument("--loose", action="store_true",
                    help="also show headlines the search returned that never name the stock")
    ap.add_argument("--portfolio", default="nse/portfolio.json")
    ap.add_argument("--watchlist", default="nse/watchlist.json")
    ap.add_argument("--plan", default="nse/plan.json")
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--per-ticker", type=int, default=PER_TICKER, help="newest headlines kept per ticker (0 = all)")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help='India time "2026-10-26 10:15" (or ...Z), for reproducible runs')
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.symbols:
        args.tickers = list(args.symbols) + list(args.tickers or [])

    try:
        if args.tickers:
            watch = {norm(t): None for t in args.tickers}
            known = watch_from_files(args.portfolio, args.watchlist, args.plan)
            watch.update({t: known[t] for t in watch if known.get(t)})
        else:
            watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
        if not watch:
            print("news: no tickers. Pass --tickers, or run /nse-scanner first (it writes "
                  "nse/watchlist.json).", file=sys.stderr)
            return 2
        now = parse_now(args.now).replace(tzinfo=datetime.timezone.utc)
    except IndiaError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    state_dir = os.path.dirname(args.state) or "."
    fill_names(fetcher, watch, state_dir, to_ist(now).date(), errors)
    items = collect(fetcher, watch, hours=args.hours, per_ticker=args.per_ticker, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    hidden_loose = 0
    if not args.loose:
        # hidden: the headline names none of your stocks (it only came back from a loose search)
        hidden_loose = sum(1 for i in shown if not i.get("mentioned", i["touches"]))
        shown = [i for i in shown if i.get("mentioned", i["touches"])]
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors, "not_covered": NOT_COVERED}, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct, links=args.links, hidden_loose=hidden_loose))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
