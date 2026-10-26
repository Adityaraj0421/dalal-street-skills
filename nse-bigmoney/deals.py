#!/usr/bin/env python3
"""NSE bulk and block deal tracker for nse-bigmoney. Standard library only.

Reads the two public CSV files NSE posts for the last trading day and keeps
every day it sees, so a history builds up: who bought or sold what, how many
shares, at what price, worth how many crore.

    python3 deals.py new                          # deals not shown before, flags your stocks
    python3 deals.py new --client "quant mutual"  # only a client name containing this
    python3 deals.py top --days 5 --n 10          # biggest deals by value in the last 5 stored days
    python3 deals.py who "Nippon" --days 30       # one client's buys and sells across stored history
    python3 deals.py show --symbol RELIANCE       # every stored deal in a stock

Where the data comes from (open archive files, no key):
  https://archives.nseindia.com/content/equities/bulk.csv
  https://archives.nseindia.com/content/equities/block.csv
Each file holds only the latest trading day and is replaced the next day. This
tool does not backfill: a day you do not run it is a day missing from your
history (NSE's website has a manual historical download, see SKILL.md).

What these are, and are not:
  - BULK deal: a client's total traded in one stock in a day is above 0.5% of
    the company's listed shares. BLOCK deal: one big order done in NSE's
    special block windows (a minimum order size applies). Both are PUBLIC and
    published after the market closes, so you read them the evening of the day,
    or the next morning.
  - The client is whoever the deal was booked under: often a fund or a rich
    investor, but also brokers, prop desks, arbitrage and market-making firms,
    and promoters. A buy is not a verdict on the stock, and the same client may
    sit on both sides in one day.
  - Insider (SEBI PIT) disclosures and shareholding patterns are NOT in these
    files and are not scraped: see SKILL.md for where to read them.

State: nse/state/deals.json holds every deal seen (the history) and which ones
were already shown, so each deal alerts once. Offline: --offline DIR reads
bulk.csv and block.csv from DIR. The tests use it.
"""

import argparse
import csv
import hashlib
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



# ---------------------------------------------------------------- deals
# Everything below is specific to nse-bigmoney.

BULK_URL = "https://archives.nseindia.com/content/equities/bulk.csv"
BLOCK_URL = "https://archives.nseindia.com/content/equities/block.csv"
CRORE = 1e7
STATE_DEFAULT = "nse/state/deals.json"
KINDS = ("bulk", "block")
MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
LIMIT_DEFAULT = 40
CAVEAT = ("Public bulk/block deal data, posted after the close. The client may be a fund, a rich "
          "investor, a broker or a trading desk; a deal says what was done, not what comes next.")


class DealsError(Exception):
    pass


def download(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/csv,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise DealsError("HTTP %s for %s" % (exc.code, url))
    except urllib.error.URLError as exc:
        raise DealsError("could not reach %s: %s" % (url, exc.reason))
    except OSError as exc:
        raise DealsError("network error for %s: %s" % (url, exc))


def parse_nse_date(text):
    """'23-OCT-2026' -> '2026-10-23' (None when it is not a date)."""
    m = re.match(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s*$", text or "")
    if not m or m.group(2).upper() not in MONTHS:
        return None
    try:
        return datetime.date(int(m.group(3)), MONTHS[m.group(2).upper()], int(m.group(1))).isoformat()
    except ValueError:
        return None


def clean(text):
    """Collapse the runs of spaces NSE leaves inside names."""
    return " ".join((text or "").split())


def _num(text):
    try:
        return float(str(text).replace(",", "").strip())
    except ValueError:
        return None


def deal_id(d, n):
    key = "|".join([d["date"], d["kind"], d["symbol"], d["client"], d["side"],
                    str(d["qty"]), "%.2f" % d["price"], str(n)])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def parse_deals(text, kind):
    """Rows of bulk.csv / block.csv as deal dicts. "NO RECORDS" gives an empty list.

    Raises DealsError if the text is not one of these files (an HTML error page, say)."""
    if kind not in KINDS:
        raise DealsError("kind is bulk or block, not %r" % kind)
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise DealsError("the %s file is empty" % kind)
    head = [clean(h).lower() for h in rows[0]]
    need = ("date", "symbol", "client name", "buy/sell", "quantity traded")
    if not all(n in head for n in need):
        raise DealsError("the %s file does not look like NSE's deal CSV (header: %s)" % (
            kind, ",".join(head)[:80]))
    col = {h: i for i, h in enumerate(head)}
    price_col = next((i for h, i in col.items() if h.startswith("trade price")), None)
    out, seen_n = [], {}
    for r in rows[1:]:
        if not r or not any(x.strip() for x in r) or clean(r[0]).upper() == "NO RECORDS":
            continue
        if len(r) < len(head) - 1:
            continue
        date = parse_nse_date(r[col["date"]])
        qty, price = _num(r[col["quantity traded"]]), _num(r[price_col]) if price_col is not None else None
        side = clean(r[col["buy/sell"]]).upper()
        sym = clean(r[col["symbol"]]).upper()
        if not (date and sym and qty and price is not None) or side not in ("BUY", "SELL"):
            continue
        d = {"date": date, "kind": kind, "symbol": sym,
             "name": clean(r[col["security name"]]) if "security name" in col else sym,
             "client": clean(r[col["client name"]]), "side": side, "qty": int(qty),
             "price": round(price, 2), "value": round(qty * price, 2),
             "remarks": clean(r[col["remarks"]]) if "remarks" in col and len(r) > col["remarks"] else ""}
        if d["remarks"] == "-":
            d["remarks"] = ""
        key = (d["date"], d["kind"], d["symbol"], d["client"], d["side"], d["qty"], d["price"])
        seen_n[key] = seen_n.get(key, 0) + 1
        d["id"] = deal_id(d, seen_n[key])
        d["exchange"], d["yahoo"] = "NSE", sym + ".NS"
        out.append(d)
    return out


def load_sources(offline=None, pause=0.5):
    """(bulk deals, block deals) from NSE, or from DIR/bulk.csv and DIR/block.csv."""
    out = []
    for i, (kind, url) in enumerate((("bulk", BULK_URL), ("block", BLOCK_URL))):
        if offline:
            path = os.path.join(offline, kind + ".csv")
            if not os.path.isfile(path):
                raise DealsError("offline: no file %s" % path)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        else:
            if i:
                time.sleep(pause)
            text = download(url)
        out.append(parse_deals(text, kind))
    return out


# ---------------------------------------------------------------- state

def load_state(path):
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                st = json.load(fh)
        except ValueError:
            raise DealsError("%s is not valid JSON: fix or delete it" % path)
        st.setdefault("deals", [])
        st.setdefault("seen", [])
        st.setdefault("last_check", None)
        return st
    return {"deals": [], "seen": [], "last_check": None}


def save_state(path, state):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
        fh.write("\n")


def merge(state, deals):
    """Add deals not stored yet to the history. Returns how many were added."""
    have = {d["id"] for d in state["deals"]}
    added = 0
    for d in deals:
        if d["id"] not in have:
            state["deals"].append(d)
            have.add(d["id"])
            added += 1
    state["deals"].sort(key=lambda d: (d["date"], d["kind"], d["symbol"], d["id"]))
    return added


def days_in(deals):
    return sorted({d["date"] for d in deals})


# ---------------------------------------------------------------- selection

def _norm(s):
    return " ".join(str(s).upper().replace(".", " ").split())


def client_match(deal, needles):
    if not needles:
        return True
    c = _norm(deal["client"])
    return any(_norm(n) in c for n in needles)


def select(deals, clients=None, symbols=None, kind=None, side=None, min_crore=None, days=None,
           on=None):
    """Filter deals. days = the last N stored days; on = one date."""
    pool = deals
    if days:
        keep = set(days_in(deals)[-days:])
        pool = [d for d in pool if d["date"] in keep]
    if on:
        pool = [d for d in pool if d["date"] == on]
    syms = {s.upper() for s in (symbols or [])}
    out = []
    for d in pool:
        if kind and d["kind"] != kind:
            continue
        if side and d["side"] != side:
            continue
        if syms and d["symbol"] not in syms:
            continue
        if min_crore and d["value"] < min_crore * CRORE:
            continue
        if not client_match(d, clients):
            continue
        out.append(d)
    return out


def combine(deals):
    """One line per (date, kind, symbol, client, side): the quantity added up, the price
    weighted by quantity. NSE often splits one client's day into several rows."""
    agg = {}
    for d in deals:
        k = (d["date"], d["kind"], d["symbol"], d["client"], d["side"])
        a = agg.get(k)
        if a is None:
            a = agg[k] = dict(d, qty=0, value=0.0, rows=0)
        a["qty"] += d["qty"]
        a["value"] += d["value"]
        a["rows"] += 1
    out = []
    for a in agg.values():
        a["price"] = round(a["value"] / a["qty"], 2) if a["qty"] else 0.0
        a["value"] = round(a["value"], 2)
        out.append(a)
    return out


def by_value(deals):
    return sorted(deals, key=lambda d: (-d["value"], d["symbol"], d["client"]))


def who(deals, client_needles):
    """A client's activity per stock across the deals given."""
    mine = [d for d in deals if client_match(d, client_needles)]
    names = sorted({d["client"] for d in mine})
    stocks = {}
    for d in mine:
        s = stocks.setdefault(d["symbol"], {"symbol": d["symbol"], "name": d["name"],
                                            "buy_qty": 0, "buy_value": 0.0,
                                            "sell_qty": 0, "sell_value": 0.0,
                                            "dates": set(), "deals": 0})
        key = "buy" if d["side"] == "BUY" else "sell"
        s[key + "_qty"] += d["qty"]
        s[key + "_value"] += d["value"]
        s["dates"].add(d["date"])
        s["deals"] += 1
    rows = []
    for s in stocks.values():
        s["net_qty"] = s["buy_qty"] - s["sell_qty"]
        s["net_value"] = round(s["buy_value"] - s["sell_value"], 2)
        s["buy_value"], s["sell_value"] = round(s["buy_value"], 2), round(s["sell_value"], 2)
        s["dates"] = sorted(s["dates"])
        rows.append(s)
    rows.sort(key=lambda s: -(s["buy_value"] + s["sell_value"]))
    return {"clients": names, "stocks": rows, "days": days_in(mine),
            "bought": round(sum(s["buy_value"] for s in rows), 2),
            "sold": round(sum(s["sell_value"] for s in rows), 2)}


# ---------------------------------------------------------------- your stocks

def _base(sym):
    s = str(sym).strip().upper()
    for suf in (".NS", ".BO"):
        if s.endswith(suf):
            s = s[:-len(suf)]
    if s.startswith(("NSE:", "BSE:")):
        s = s[4:]
    return s


def _collect(node, out):
    """Symbols anywhere in a JSON document: values of symbol/ticker/tradingsymbol keys and
    string items of lists named symbols/tickers/watchlist."""
    if isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower()
            if kl in ("symbol", "ticker", "tradingsymbol", "yahoo") and isinstance(v, str):
                out.add(_base(v))
            elif kl in ("symbols", "tickers", "watchlist", "holdings", "positions") and isinstance(v, list):
                for x in v:
                    if isinstance(x, str):
                        out.add(_base(x))
                    else:
                        _collect(x, out)
            else:
                _collect(v, out)
    elif isinstance(node, list):
        for x in node:
            if isinstance(x, str):
                continue
            _collect(x, out)


def my_stocks(portfolio, watchlist, extra=()):
    """{symbol: 'holding' | 'watchlist' | 'asked'}; a holding beats a watchlist entry."""
    mine = {}
    for label, path in (("watchlist", watchlist), ("holding", portfolio)):
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                continue
            found = set()
            _collect(doc, found)
            for s in found:
                if s:
                    mine[s] = label
    for s in extra or ():
        mine.setdefault(_base(s), "asked")
    return mine


# ---------------------------------------------------------------- output

def money(v):
    return lakh_crore(v)


def line(d, mine=None):
    n = d.get("rows", 1)
    s = "%s %-5s %-5s %-12s %-34s %13s @ %s = %s" % (
        d["date"], d["kind"], d["side"], d["symbol"], d["client"][:34], group_in(d["qty"]),
        inr(d["price"]), money(d["value"]))
    if n > 1:
        s += " (%d rows)" % n
    flag = (mine or {}).get(d["symbol"])
    if flag:
        s += "  <- %s is on your %s" % (d["symbol"], {"holding": "holdings", "watchlist": "watchlist",
                                                       "asked": "list"}[flag])
    return s


def render_deals(deals, mine=None, limit=LIMIT_DEFAULT):
    out = []
    shown = deals if not limit else deals[:limit]
    for d in shown:
        out.append("  " + line(d, mine))
    if limit and len(deals) > limit:
        out.append("  ... and %d more (use --limit 0 for all)" % (len(deals) - limit))
    return "\n".join(out) + ("\n" if out else "")


def freshness(deals, today):
    """Plain sentence about how old the newest data is."""
    ds = days_in(deals)
    if not ds:
        return None
    last = datetime.date.fromisoformat(ds[-1])
    age = (today - last).days
    if age == 0:
        return "Newest deals are from today (%s)." % ds[-1]
    return "Newest deals are from %s, %d day%s ago (NSE posts them after the close; the files hold one trading day)." % (
        ds[-1], age, "" if age == 1 else "s")


def history_note(state):
    ds = days_in(state["deals"])
    if not ds:
        return "No deals stored yet."
    if len(ds) == 1:
        return ("History holds 1 day (%s). Run this each trading evening to build it; days you skip "
                "are not recoverable from these files." % ds[0])
    return "History holds %d days, %s to %s (days you did not run it are missing)." % (len(ds), ds[0], ds[-1])


def render_who(res, label):
    out = []
    if not res["stocks"]:
        return "No deals by %r in the stored history.\n" % label
    out.append("%s: %s" % (label, "; ".join(res["clients"][:6]) + (" ..." if len(res["clients"]) > 6 else "")))
    out.append("Bought %s, sold %s across %d stock%s on %d day%s." % (
        money(res["bought"]), money(res["sold"]), len(res["stocks"]),
        "" if len(res["stocks"]) == 1 else "s", len(res["days"]), "" if len(res["days"]) == 1 else "s"))
    out.append("")
    out.append("%-12s %-30s %16s %16s %10s" % ("stock", "name", "bought", "sold", "net"))
    for s in res["stocks"]:
        out.append("%-12s %-30s %16s %16s %10s" % (
            s["symbol"], s["name"][:30], money(s["buy_value"]) if s["buy_value"] else "-",
            money(s["sell_value"]) if s["sell_value"] else "-",
            ("net buyer" if s["net_value"] > 0 else "net seller" if s["net_value"] < 0 else "flat")))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- CLI

def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    def common(p, fetch=True):
        p.add_argument("--state", default=STATE_DEFAULT)
        p.add_argument("--offline", metavar="DIR", help="read bulk.csv and block.csv from DIR")
        p.add_argument("--no-fetch", action="store_true", help="use stored history only")
        p.add_argument("--now", help="India time, e.g. '2026-10-26 18:30'")
        p.add_argument("--dry-run", action="store_true", help="do not write the state file")
        p.add_argument("--json", action="store_true")
        p.add_argument("--portfolio", default="nse/portfolio.json")
        p.add_argument("--watchlist", default="nse/watchlist.json")
        p.add_argument("--tickers", nargs="*", help="flag deals in these stocks too")

    def filters(p):
        p.add_argument("--client", action="append", help="client name contains this; repeat for more")
        p.add_argument("--symbol", action="append", help="only this stock; repeat for more")
        p.add_argument("--kind", choices=KINDS)
        p.add_argument("--side", choices=("BUY", "SELL"))
        p.add_argument("--min-crore", type=float, help="only deals worth at least this many crore")

    p = sub.add_parser("new", help="deals not shown before")
    common(p)
    filters(p)
    p.add_argument("--limit", type=int, default=LIMIT_DEFAULT, help="rows to print (0 = all)")
    p.add_argument("--raw", action="store_true", help="every CSV row, not one line per client/side")

    p = sub.add_parser("show", help="stored deals, newest day first")
    common(p)
    filters(p)
    p.add_argument("--days", type=int, default=1, help="last N stored days (default 1)")
    p.add_argument("--limit", type=int, default=LIMIT_DEFAULT)

    p = sub.add_parser("top", help="biggest deals by value")
    common(p)
    filters(p)
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--n", type=int, default=10)

    p = sub.add_parser("who", help="one client's buys and sells across stored history")
    common(p)
    p.add_argument("client", nargs="+", help="client name or part of it")
    p.add_argument("--days", type=int, help="last N stored days (default: all)")
    return ap


def main(argv=None):
    utf8_stdout()
    ap = _parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    try:
        return _run(args)
    except (DealsError, IndiaError, OSError) as exc:
        print("deals: %s" % exc, file=sys.stderr)
        return 2


def _run(args):
    now = parse_now(args.now)
    today = to_ist(now).date()
    state = load_state(args.state)
    fetched = []
    if not args.no_fetch:
        bulk, block = load_sources(args.offline)
        fetched = bulk + block
        merge(state, fetched)
    if not state["deals"]:
        print("No deals stored and none fetched. NSE's files can be empty ('NO RECORDS') on days with "
              "no such deals.")
        return 1
    mine = my_stocks(args.portfolio, args.watchlist, args.tickers)
    fresh_line = freshness(state["deals"], today)
    notes = [fresh_line, history_note(state)]
    clients = getattr(args, "client", None)

    if args.cmd == "who":
        pool = select(state["deals"], days=args.days)
        res = who(pool, args.client)
        _save(args, state, [])
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            sys.stdout.write(render_who(res, " ".join(args.client)))
            for n in notes:
                if n:
                    print(n)
            print(CAVEAT)
        return 0 if res["stocks"] else 1

    kw = dict(clients=clients, symbols=args.symbol, kind=args.kind, side=args.side,
              min_crore=args.min_crore)
    if args.cmd == "new":
        seen = set(state["seen"])
        # a deal row is "new" when it was never shown (filtered-out deals stay unseen)
        pool = [d for d in state["deals"] if d["id"] not in seen]
        picked = select(pool, **kw)
        shown = picked if args.raw else combine(picked)
        shown = by_value(shown)
        _save(args, state, picked)
        if args.json:
            print(json.dumps({"new": shown, "notes": [n for n in notes if n]}, indent=2))
        else:
            if not shown:
                print("Nothing new since %s." % (state.get("last_check") or "the last check"))
            else:
                print("NEW deals: %d row%s, %d line%s, worth %s." % (
                    len(picked), "" if len(picked) == 1 else "s", len(shown),
                    "" if len(shown) == 1 else "s", money(sum(d["value"] for d in picked))))
                sys.stdout.write(render_deals(shown, mine, args.limit))
                flagged = sorted({d["symbol"] for d in shown if d["symbol"] in mine})
                if flagged:
                    print("On your lists: %s." % ", ".join(flagged))
            for n in notes:
                if n:
                    print(n)
            print(CAVEAT)
        return 0 if shown else 1

    pool = select(state["deals"], days=args.days, **kw)
    rows = by_value(combine(pool))
    if args.cmd == "top":
        rows = rows[:args.n]
        _save(args, state, [])
    else:
        rows = sorted(rows, key=lambda d: (d["date"], d["value"]), reverse=True)
        _save(args, state, [])
    if args.json:
        print(json.dumps({"deals": rows, "notes": [n for n in notes if n]}, indent=2))
    else:
        if not rows:
            print("No stored deals match.")
        else:
            sys.stdout.write(render_deals(rows, mine, args.limit if args.cmd == "show" else 0))
        for n in notes:
            if n:
                print(n)
        print(CAVEAT)
    return 0 if rows else 1


def _save(args, state, shown):
    """Write the history, and mark the deals just shown as seen."""
    if args.dry_run:
        return
    if shown:
        seen = set(state["seen"])
        seen.update(d["id"] for d in shown)
        state["seen"] = sorted(seen)
        state["last_check"] = to_ist(utc_now()).replace(microsecond=0).isoformat(sep=" ") + " IST"
    save_state(args.state, state)


if __name__ == "__main__":
    sys.exit(main())
