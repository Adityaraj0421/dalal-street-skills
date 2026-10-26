#!/usr/bin/env python3
"""Results reader for nse-analyst. Standard library only.

Pulls a listed Indian company's reported numbers from Yahoo's fundamentals
time series and lays out what changed, quarter by quarter, in rupees crore:
revenue, gross, operating, EBITDA and net margins, net profit, EPS, and growth
against the same quarter a year earlier, plus the trailing twelve months and
the last few fiscal years (with balance sheet and cash flow lines).

    python3 facts.py RELIANCE
    python3 facts.py RELIANCE TCS --quarters 8
    python3 facts.py INFY.NS --json
    python3 facts.py RELIANCE TCS INFY --metrics nse/metrics.json

Source (open, no key): Yahoo
  https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/<SYM>.NS?type=...
Values are in the currency the company reports in, which is rupees for most
Indian companies but US dollars for some (Infosys, for one). The table header
says which; rupee signs are never put on dollar figures. --inr converts to
rupees at each quarter end's USD/INR close (Yahoo USDINR=X). The Indian fiscal year runs April to March: the quarter
ending 30 June 2026 is Q1FY27, 31 March 2027 is Q4FY27.

Honesty rules built in:
  - Yahoo often lacks quarters (a reported quarter can simply be absent). A
    missing quarter is shown as a gap, never interpolated or estimated.
  - Growth is against exactly the same quarter a year earlier; if that one is
    absent there is no growth number.
  - Trailing twelve months only when four consecutive quarters exist.
  - Yahoo is a third-party copy. The official numbers are the results filing on
    NSE or BSE and the company's investor page: this tool does not scrape
    nseindia.com or bseindia.com (bot-protected) and says where to look.

--metrics FILE merges one row per ticker into a JSON file. Offline: --offline
DIR reads <SYMBOL>.NS.json (the saved Yahoo response) from DIR.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
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



# ---------------------------------------------------------------- analyst
# Everything below is specific to nse-analyst.

CRORE = 1e7
Q_KEYS = {"revenue": "TotalRevenue", "cost": "CostOfRevenue", "gross": "GrossProfit",
          "operating": "OperatingIncome", "ebitda": "EBITDA", "net": "NetIncome",
          "eps_diluted": "DilutedEPS", "eps_basic": "BasicEPS"}
A_KEYS = dict(Q_KEYS, debt="TotalDebt", equity="StockholdersEquity",
              cash="CashAndCashEquivalents", ocf="OperatingCashFlow",
              fcf="FreeCashFlow", capex="CapitalExpenditure", assets="TotalAssets")
WINDOW_YEARS = 6
CCY_KEY = "__currency__"       # {type: {date: "INR" | "USD" ...}} read from each data point
SYMBOLS = {"INR": "₹", "USD": "US$"}
OFFICIAL = ("Official numbers: the results filing on NSE (nseindia.com > Corporate Filings > "
            "Financial Results) or BSE (bseindia.com > Corporate Announcements > Results), or the "
            "company's investor page. Yahoo is a third-party copy: it does not say whether a "
            "figure is consolidated or standalone, so check before quoting.")


def type_list():
    return ["quarterly" + v for v in Q_KEYS.values()] + ["annual" + v for v in A_KEYS.values()]


def fundamentals_path(ysym, now_utc):
    p1 = ts_of(now_utc - datetime.timedelta(days=365 * WINDOW_YEARS))
    p2 = ts_of(now_utc + datetime.timedelta(days=1))
    return "/ws/fundamentals-timeseries/v1/finance/timeseries/%s?type=%s&period1=%d&period2=%d" % (
        yahoo_quote(ysym), ",".join(type_list()), p1, p2)


def parse_timeseries(doc):
    """Yahoo response -> {"quarterlyTotalRevenue": {"2026-06-30": 3094680000000.0, ...}, ...}"""
    ts = (doc or {}).get("timeseries") or {}
    err = ts.get("error")
    if err:
        raise IndiaError("Yahoo: %s" % (err.get("description") or err.get("code") or err))
    out, ccy = {}, {}
    for r in ts.get("result") or []:
        types = (r.get("meta") or {}).get("type") or []
        for t in types:
            pts, cc = {}, {}
            for p in r.get(t) or []:
                raw = (p.get("reportedValue") or {}).get("raw")
                if p.get("asOfDate") and isinstance(raw, (int, float)):
                    pts[p["asOfDate"]] = float(raw)
                    cc[p["asOfDate"]] = str(p.get("currencyCode") or "INR").upper()
            if pts:
                out[t], ccy[t] = pts, cc
    out[CCY_KEY] = ccy
    return out


def load_fundamentals(ysym, now_utc, offline=None):
    if offline:
        path = os.path.join(offline, "%s.json" % ysym)
        if not os.path.isfile(path):
            raise IndiaError("offline: no file %s" % path)
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    else:
        try:
            doc = json.loads(yahoo_get(fundamentals_path(ysym, now_utc)))
        except ValueError:
            raise IndiaError("Yahoo sent something that is not JSON for %s" % ysym)
    data = parse_timeseries(doc)
    if not any(k.startswith("quarterly") or k.startswith("annual") for k in data if k != CCY_KEY):
        raise IndiaError("Yahoo has no results data for %s (unknown symbol, a very new listing, or "
                         "no filings on Yahoo): check the NSE/BSE results filings" % ysym)
    return data


# ---- currency
# Yahoo marks every data point with a currencyCode. Some Indian companies report in
# US dollars (Infosys). Their numbers are never shown with a rupee sign.

def _ccy_of(data, key, date_str):
    return (data.get(CCY_KEY) or {}).get(key, {}).get(date_str, "INR")


def reporting_currency(data):
    """Currency of the newest revenue (else net profit) point."""
    for key in ("quarterlyTotalRevenue", "quarterlyNetIncome", "annualTotalRevenue", "annualNetIncome"):
        pts = data.get(key)
        if pts:
            return _ccy_of(data, key, max(pts))
    return "INR"


def foreign_currencies(data):
    return sorted({c for per in (data.get(CCY_KEY) or {}).values() for c in per.values()} - {"INR"})


def unify_currency(data):
    """-> (data in one currency, currency, notes). Points in any other currency than the
    stock's reporting currency are dropped, and the notes say so (never mixed in)."""
    cur = reporting_currency(data)
    notes, out, ccy = [], {}, {}
    for key, pts in data.items():
        if key == CCY_KEY:
            continue
        keep = {d: v for d, v in pts.items() if _ccy_of(data, key, d) == cur}
        dropped = len(pts) - len(keep)
        if dropped:
            notes.append("%s: %d point%s in another currency left out (this stock reports in %s)" % (
                key, dropped, "" if dropped == 1 else "s", cur))
        if keep:
            out[key] = keep
            ccy[key] = {d: cur for d in keep}
    out[CCY_KEY] = ccy
    return out, cur, notes


def parse_fx_chart(doc):
    """Yahoo chart JSON for a currency pair -> sorted [(date 'YYYY-MM-DD', close)]."""
    try:
        res = doc["chart"]["result"][0]
        stamps, closes = res["timestamp"], res["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        raise IndiaError("Yahoo returned no exchange-rate history")
    off = int((res.get("meta") or {}).get("gmtoffset") or 0)
    out = []
    for t, c in zip(stamps, closes):
        if c is not None:
            out.append((((EPOCH + datetime.timedelta(seconds=int(t) + off)).date()).isoformat(), float(c)))
    out.sort()
    return out


def load_fx(ccy, offline=None):
    pair = "%sINR=X" % ccy
    if offline:
        path = os.path.join(offline, pair + ".json")
        if not os.path.isfile(path):
            raise IndiaError("offline: no file %s" % path)
        with open(path, encoding="utf-8") as fh:
            return parse_fx_chart(json.load(fh))
    try:
        return parse_fx_chart(json.loads(yahoo_get(chart_path(pair, "5y", "1d"))))
    except ValueError:
        raise IndiaError("Yahoo sent something that is not JSON for %s" % pair)


def rate_on(series, date_str):
    """(rate, rate_date): the last close on or before date_str, or None."""
    best = None
    for d, c in series:
        if d > date_str:
            break
        best = (c, d)
    return best


def convert_to_inr(data, fx):
    """Convert every non-INR point at the close on or before its own date (a quarter end).
    fx = {ccy: series}. -> (data in INR, rates used {ccy: {date: [rate, rate_date]}}, notes)."""
    out, ccy, used, notes = {}, {}, {}, []
    for key, pts in data.items():
        if key == CCY_KEY:
            continue
        new = {}
        for d, v in pts.items():
            c = _ccy_of(data, key, d)
            if c == "INR":
                new[d] = v
                continue
            hit = rate_on(fx.get(c, []), d) if c in fx else None
            if hit is None:
                notes.append("%s %s: no %s/INR rate on or before that date, point left out" % (key, d, c))
                continue
            new[d] = v * hit[0]
            used.setdefault(c, {})[d] = [round(hit[0], 4), hit[1]]
        if new:
            out[key] = new
            ccy[key] = {d: "INR" for d in new}
    out[CCY_KEY] = ccy
    return out, used, notes


# ---- Indian fiscal calendar

def _d(s):
    return datetime.date.fromisoformat(s)


def qindex(date_str):
    """Quarter-end date -> a running quarter number (fiscal year * 4 + quarter - 1), or None
    when the date is not a calendar quarter end (Mar, Jun, Sep, Dec)."""
    d = _d(date_str)
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(d.month)
    if q is None or d.day < 28:
        return None
    fy = d.year + 1 if d.month >= 4 else d.year
    return fy * 4 + q - 1


def qlabel(idx):
    return "Q%dFY%02d" % (idx % 4 + 1, (idx // 4) % 100)


def qend(idx):
    """Quarter end date of a running quarter number (Q1 = Apr-Jun of fiscal year fy-1)."""
    fy, q = idx // 4, idx % 4 + 1
    month = {1: 6, 2: 9, 3: 12, 4: 3}[q]
    year = fy if q == 4 else fy - 1
    day = 30 if month in (6, 9) else 31
    return datetime.date(year, month, day)


def alabel(date_str):
    d = _d(date_str)
    if d.month == 3:
        return "FY%02d" % (d.year % 100)
    return date_str


def report_deadline(idx):
    """SEBI LODR: quarterly results within 45 days of the quarter end, 60 for the March quarter."""
    return qend(idx) + datetime.timedelta(days=60 if idx % 4 == 3 else 45)


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def quarter_series(data):
    """{metric: {idx: value}} for quarterly keys; a date that is not a quarter end is dropped."""
    s = {}
    for k, suffix in Q_KEYS.items():
        pts = {}
        for date_str, val in (data.get("quarterly" + suffix) or {}).items():
            i = qindex(date_str)
            if i is not None:
                pts[i] = val
        s[k] = pts
    return s


def analyse(data, quarters=6, now_date=None, fx_used=None, fx_notes=None):
    """Every number in the report, as a dict. Figures stay in the stock's reporting currency
    (result["currency"]); points in any other currency are left out and noted."""
    data, currency, cur_notes = unify_currency(data)
    s = quarter_series(data)
    eps = dict(s["eps_basic"])
    eps.update(s["eps_diluted"])
    s["eps"] = eps
    known = set(s["revenue"]) | set(s["net"])
    if not known:
        raise IndiaError("no quarterly revenue or net profit on Yahoo for this stock: read the "
                         "NSE/BSE results filings")
    latest = max(known)
    rows, gaps = [], []
    for idx in range(latest, latest - max(1, quarters), -1):
        if idx not in known:
            rows.append({"quarter": qlabel(idx), "idx": idx, "end": qend(idx).isoformat(),
                         "missing": True})
            gaps.append(qlabel(idx))
            continue
        r, ni = s["revenue"].get(idx), s["net"].get(idx)
        gp, gp_from_cost = s["gross"].get(idx), False
        if gp is None and r is not None and idx in s["cost"]:
            gp, gp_from_cost = r - s["cost"][idx], True
        op, eb = s["operating"].get(idx), s["ebitda"].get(idx)
        e = s["eps"].get(idx)
        prior = idx - 4
        rows.append({
            "quarter": qlabel(idx), "idx": idx, "end": qend(idx).isoformat(), "missing": False,
            "revenue": r, "gross_profit": gp, "gross_from_cost": gp_from_cost,
            "operating_income": op, "ebitda": eb, "net_income": ni,
            "eps": round(e, 2) if e is not None else None,
            "gross_margin": margin(gp, r), "operating_margin": margin(op, r),
            "ebitda_margin": margin(eb, r), "net_margin": margin(ni, r),
            "revenue_yoy": pct(r, s["revenue"].get(prior)),
            "net_income_yoy": pct(ni, s["net"].get(prior)),
            "eps_yoy": pct(e, s["eps"].get(prior)),
            "has_year_ago": prior in known,
        })
    ttm, ttm_note, blocked = {}, [], {}
    for key in ("revenue", "operating", "ebitda", "net", "eps"):
        have = s[key]
        if not have:
            continue
        top = max(have)
        window = [top - i for i in range(4)]
        missing = [qlabel(i) for i in window if i not in have]
        if missing:
            blocked.setdefault(tuple(missing), []).append(key)
            continue
        total = sum(have[i] for i in window)
        ttm[key] = round(total, 2) if key == "eps" else total
        ttm[key + "_to"] = qlabel(top)
    for missing, keys in blocked.items():
        ttm_note.append("no trailing twelve months for %s: %s missing on Yahoo" % (
            ", ".join(keys), ", ".join(missing)))
    result = {"currency": currency, "currency_notes": cur_notes + list(fx_notes or []),
              "fx_used": fx_used or {}, "rows": rows, "gaps": gaps, "ttm": ttm, "ttm_notes": ttm_note,
              "annual": annual_rows(data),
              "latest_quarter": qlabel(latest), "latest_end": qend(latest).isoformat(),
              "latest_idx": latest}
    result["stale"] = stale_note(result, now_date)
    return result


def annual_rows(data, years=4):
    ann = {}
    for k, suffix in A_KEYS.items():
        for date_str, val in (data.get("annual" + suffix) or {}).items():
            ann.setdefault(date_str, {})[k] = val
    out = []
    ends = sorted(ann)[-years:]
    for i, d in enumerate(ends):
        v = ann[d]
        prev = ann.get(ends[i - 1]) if i else None
        # only compare with a fiscal year that is exactly one year earlier
        if prev is not None and abs((_d(d) - _d(ends[i - 1])).days - 365) > 5:
            prev = None
        e = v.get("eps_diluted", v.get("eps_basic"))
        out.append({
            "year": alabel(d), "end": d, "revenue": v.get("revenue"), "net_income": v.get("net"),
            "operating_income": v.get("operating"), "ebitda": v.get("ebitda"),
            "eps": round(e, 2) if e is not None else None,
            "operating_margin": margin(v.get("operating"), v.get("revenue")),
            "net_margin": margin(v.get("net"), v.get("revenue")),
            "revenue_yoy": pct(v.get("revenue"), (prev or {}).get("revenue")),
            "net_income_yoy": pct(v.get("net"), (prev or {}).get("net")),
            "total_debt": v.get("debt"), "equity": v.get("equity"), "cash": v.get("cash"),
            "operating_cash_flow": v.get("ocf"), "free_cash_flow": v.get("fcf"),
            "capex": v.get("capex"), "total_assets": v.get("assets"),
        })
    return out


def stale_note(result, today):
    """When the latest quarter on Yahoo is older than a filing that should already be out."""
    if today is None:
        return None
    latest = result["latest_idx"]
    due = None
    for idx in range(latest + 1, latest + 6):
        if report_deadline(idx) <= today:
            due = idx
    if due is None:
        return None
    return ("Yahoo stops at %s, but %s results were due by %s (SEBI: 45 days after the quarter, 60 "
            "for the March quarter). Check the NSE/BSE results filing for the newer numbers." % (
                qlabel(latest), qlabel(due), report_deadline(due).isoformat()))


def next_due(result):
    idx = result["latest_idx"] + 1
    return qlabel(idx), report_deadline(idx).isoformat()


def changes(result):
    """Plain sentences: what changed in the latest quarter."""
    rows = result["rows"]
    if not rows or rows[0]["missing"]:
        return []
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 and not rows[1]["missing"] else None
    out = []

    ccy = result.get("currency", "INR")

    def money(v):
        if v is None:
            return "n/a"
        if ccy == "INR":
            return "%s%s crore" % (RUPEE, group_in(round(v / CRORE)))
        return "%s %s million" % (ccy, "{:,.0f}".format(v / 1e6))

    line = "%s (quarter ending %s): revenue %s" % (cur["quarter"], cur["end"], money(cur["revenue"]))
    if cur["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % cur["revenue_yoy"]
    line += ", net profit %s" % money(cur["net_income"])
    if cur["net_income_yoy"] is not None:
        line += " (%+.1f%%)" % cur["net_income_yoy"]
    if cur["eps"] is not None:
        line += ", EPS %s" % eps_text(cur["eps"], ccy)
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%)" % cur["eps_yoy"]
    out.append(line + ".")
    if not cur["has_year_ago"]:
        out.append("The same quarter a year earlier (%s) is not on Yahoo, so there is no "
                   "year-on-year growth number. None was estimated." % qlabel(cur["idx"] - 4))
    for key, name in (("gross_margin", "Gross margin"), ("operating_margin", "Operating margin"),
                      ("ebitda_margin", "EBITDA margin"), ("net_margin", "Net margin")):
        if cur[key] is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if prev and prev[key] is not None:
            s += " (%+.1f points vs the quarter before)" % (cur[key] - prev[key])
        elif not prev and len(rows) > 1:
            s += " (the quarter before is not on Yahoo)"
        out.append(s + ".")
    if cur["net_income"] is not None and cur["operating_income"] is not None \
            and cur["net_income"] > cur["operating_income"]:
        out.append("Net profit is above operating income: it includes income outside the core "
                   "business (treasury or investment income, exceptional or tax items). Read the "
                   "results filing before treating it as growth.")
    if prev and cur["revenue"] is not None and prev["revenue"]:
        out.append("Revenue %+.1f%% vs the quarter before (many Indian businesses are seasonal: "
                   "festive demand, monsoon, year-end billing; compare with a year earlier first)." %
                   pct(cur["revenue"], prev["revenue"]))
    return out


# ---------------------------------------------------------------- output

def make_cr(ccy):
    """Number formatter: rupees crore with Indian grouping, other currencies in millions."""
    def f(v):
        if v is None:
            return "-"
        if ccy == "INR":
            n = int(round(v / CRORE))
            return ("-" if n < 0 else "") + group_in(abs(n))
        return "{:,.0f}".format(v / 1e6)
    return f


def unit_text(ccy):
    return "%s crore" % RUPEE if ccy == "INR" else "%s million" % ccy


def eps_text(v, ccy):
    return inr(v) if ccy == "INR" else "%s %.2f" % (ccy, v)


def _p(v):
    return "-" if v is None else ("%+.1f%%" % v if v else "0.0%")


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def render(ticker, result):
    ccy = result["currency"]
    _cr = make_cr(ccy)
    head = "%s. Amounts in %s except EPS (%s)." % (ticker, unit_text(ccy), ccy if ccy != "INR" else RUPEE)
    if ccy != "INR":
        head += (" This company reports in %s, not rupees: no rupee sign is used. Add --inr to convert "
                 "at each quarter end's rate." % ccy)
    if result["fx_used"]:
        head += " Converted from %s at period-end rates (see below)." % ", ".join(sorted(result["fx_used"]))
    out = [head + " Source: Yahoo fundamentals; Indian fiscal year (April-March).", ""]
    out.append("%-8s %-10s %10s %8s %7s %7s %7s %7s %10s %8s %8s %7s" % (
        "quarter", "ends", "revenue", "rev yoy", "gross", "oper", "EBITDA", "net", "net profit",
        "NP yoy", "EPS", "EPS yoy"))
    for r in result["rows"]:
        if r["missing"]:
            out.append("%-8s %-10s  not on Yahoo (a gap, not a zero)" % (r["quarter"], r["end"]))
            continue
        out.append("%-8s %-10s %10s %8s %7s %7s %7s %7s %10s %8s %8s %7s" % (
            r["quarter"], r["end"], _cr(r["revenue"]), _p(r["revenue_yoy"]), _pm(r["gross_margin"]),
            _pm(r["operating_margin"]), _pm(r["ebitda_margin"]), _pm(r["net_margin"]),
            _cr(r["net_income"]), _p(r["net_income_yoy"]),
            "-" if r["eps"] is None else "%.2f" % r["eps"], _p(r["eps_yoy"])))
    t = result["ttm"]
    out.append("")
    if "revenue" in t or "net" in t:
        suffix = " crore" if ccy == "INR" else " " + ccy + " million"
        out.append("Last 4 quarters (to %s): revenue %s%s, net profit %s%s, EPS %s." % (
            t.get("revenue_to") or t.get("net_to"),
            _cr(t.get("revenue")), suffix, _cr(t.get("net")), suffix,
            eps_text(t["eps"], ccy) if t.get("eps") is not None else "-"))
    else:
        out.append("Last 4 quarters: not shown, four consecutive quarters are not all on Yahoo.")
    for n in result["ttm_notes"]:
        out.append("  (%s)" % n)
    if result["gaps"]:
        out.append("Gaps: %s not on Yahoo for this stock. Nothing was filled in." % ", ".join(result["gaps"]))
    if result["annual"]:
        out.append("")
        out.append("Fiscal years (%s):" % unit_text(ccy))
        for a in result["annual"]:
            out.append("  %-10s rev %10s (%s)  NP %9s (%s)  EPS %7s  op %6s  OCF %9s  FCF %9s  "
                       "debt %10s  equity %10s" % (
                           a["year"], _cr(a["revenue"]), _p(a["revenue_yoy"]), _cr(a["net_income"]),
                           _p(a["net_income_yoy"]), "-" if a["eps"] is None else "%.2f" % a["eps"],
                           _pm(a["operating_margin"]), _cr(a["operating_cash_flow"]),
                           _cr(a["free_cash_flow"]), _cr(a["total_debt"]), _cr(a["equity"])))
    if result["fx_used"]:
        out.append("")
        for c, per in sorted(result["fx_used"].items()):
            ds = sorted(per)
            out.append("Converted from %s to rupees at the %s/INR close on or before each period end "
                       "(%s: %.2f ... %s: %.2f). Growth rates now include currency moves." % (
                           c, c, ds[0], per[ds[0]][0], ds[-1], per[ds[-1]][0]))
    for n in result["currency_notes"]:
        out.append("  (%s)" % n)
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    nq, nd = next_due(result)
    out.append("")
    out.append("Next results: %s, due by %s at the latest (the exact board-meeting date is on the "
               "NSE/BSE announcements or the company's investor page)." % (nq, nd))
    if result["stale"]:
        out.append("")
        out.append("NOTE: " + result["stale"])
    out.append("")
    out.append(OFFICIAL)
    return "\n".join(out) + "\n"


def metrics_row(ticker, sym, result):
    rows = [r for r in result["rows"] if not r["missing"]]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    return {"ticker": ticker, "exchange": sym["exchange"], "yahoo": sym["yahoo"],
            "period_end": cur.get("end"), "quarter": cur.get("quarter"),
            "revenue_ttm": t.get("revenue"), "net_income_ttm": t.get("net"),
            "eps_ttm": t.get("eps"),
            "revenue_growth_yoy": cur.get("revenue_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "net_income_growth_yoy": cur.get("net_income_yoy"),
            "gross_margin": cur.get("gross_margin"), "operating_margin": cur.get("operating_margin"),
            "net_margin": cur.get("net_margin"), "currency": result["currency"],
            "source": "Yahoo fundamentals timeseries"}


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tickers", nargs="*", help="NSE symbols, e.g. RELIANCE TCS INFY.NS RELIANCE.BO")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per ticker into this JSON")
    ap.add_argument("--now", help="India time, e.g. '2026-10-26 10:00' (for reproducible runs)")
    ap.add_argument("--offline", metavar="DIR",
                    help="read <SYMBOL>.NS.json (and USDINR=X.json for --inr) from this folder")
    ap.add_argument("--inr", action="store_true",
                    help="convert companies that report in another currency (Infosys: USD) to rupees "
                         "at each period end's Yahoo exchange-rate close")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.tickers:
        ap.print_help()
        return 2
    try:
        now = parse_now(args.now)
    except IndiaError as exc:
        print("facts: %s" % exc, file=sys.stderr)
        return 2
    today = to_ist(now).date()
    results, rows, failed = [], [], 0
    for t in args.tickers:
        try:
            sym = parse_symbol(t)
            if sym["index"]:
                raise IndiaError("%s is an index: company results need a stock symbol" % t)
            if not args.offline and results:
                time.sleep(0.4)
            data = load_fundamentals(sym["yahoo"], now, args.offline)
            fx_used, fx_notes = {}, []
            if args.inr and foreign_currencies(data):
                fx = {}
                for c in foreign_currencies(data):
                    if not args.offline and fx:
                        time.sleep(0.4)
                    fx[c] = load_fx(c, args.offline)
                data, fx_used, fx_notes = convert_to_inr(data, fx)
            result = analyse(data, quarters=args.quarters, now_date=today, fx_used=fx_used,
                             fx_notes=fx_notes)
        except IndiaError as exc:
            print("facts: %s: %s" % (t.upper(), exc), file=sys.stderr)
            failed += 1
            continue
        name = sym["tradingsymbol"]
        results.append({"ticker": name, "yahoo": sym["yahoo"], "result": result,
                        "changes": changes(result), "note": result["stale"]})
        rows.append(metrics_row(name, sym, result))
        if not args.json:
            sys.stdout.write(render(name, result))
            sys.stdout.write("\n")
    if args.json:
        print(json.dumps(results, indent=2))
    if args.metrics and rows:
        existing = {}
        if os.path.isfile(args.metrics):
            with open(args.metrics, encoding="utf-8") as fh:
                existing = {r["ticker"]: r for r in json.load(fh).get("companies", [])}
        for r in rows:
            existing[r["ticker"]] = r
        folder = os.path.dirname(args.metrics)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.metrics, "w", encoding="utf-8") as fh:
            json.dump({"as_of": today.isoformat(),
                       "companies": sorted(existing.values(), key=lambda r: r["ticker"])},
                      fh, indent=2)
            fh.write("\n")
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, len(existing)))
    return 0 if results and not failed else (1 if results else 2)


if __name__ == "__main__":
    sys.exit(main())
