#!/usr/bin/env python3
"""Pre-trade risk check for nse-risk. Standard library only.

Reads the proposed orders (nse/orders.json) and checks every one against
your rules (nse/config.json), your account (nse/portfolio.json), prices
(nse/prices.json), the NSE series / price-band list (sec_list.csv) and what
was placed today (nse/state/broker.json). Each order gets PASS or BLOCK with
the reasons. Orders are checked in file order as if each PASS had filled, so
buys that are fine one by one but break a limit together are caught.

    python3 check.py                                 # the nse/ files, writes nse/state/risk.json
    python3 check.py --now "2026-10-26 09:45"        # pin the clock (India time, IST)
    python3 check.py --orders nse/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts
    python3 check.py --offline --seclist sec_list.csv   # no network at all

Network use (only without --offline, and only these): sec_list.csv from
archives.nseindia.com (cached in nse/state/ for the day) and Yahoo's daily
chart for the previous close, which the circuit band is measured from. A
previous close in nse/prices.json (detail.SYMBOL.prev_close) or in the order
(prev_close) is used first.

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        missing symbol or side, quantity not a whole number, a LIMIT
                without a price, SL / SL-M without a trigger price, an
                unknown product or order type, a stop leg that does not match
                the entry (side, quantity, product), a bad GTT OCO
  asset         anything but NSE / BSE cash equity (F&O needs "allow_fno")
  short         selling more than you hold. CNC is never allowed short (you
                cannot sell shares you do not have); MIS only with "allow_short"

What blocks an entry (on top):

  series        series BE / BZ (trade-for-trade) with MIS: delivery only
  gsm / asm     a stock under GSM / ASM (sec_list Remarks) with MIS
                (CNC only warns)
  circuit       entry, stop or target outside the price band: previous close
                x (1 +/- band%) from sec_list.csv ("No Band" stocks: warning)
  blocked       a symbol on your "blocked" list
  size          under min_order_inr, over max_order_inr (order value)
  position      the stock would tie up more than max_position_pct of the account
  cash          the margin (MIS: value / mis_leverage, CNC: full value) leaves
                less than your cash buffer or more than the cash you have
  duplicate     the same symbol, side and product twice in this batch, or
                already placed today (nse/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          MIS: no stop leg while "require_stop": true. CNC: no GTT OCO
                while "require_stop_cnc": true (default false: warning). A stop
                on the wrong side of the entry
  loss          qty x (entry - stop) PLUS the round-trip charges at the stop,
                over max_loss_per_trade_inr. The entry is the limit price for a
                limit order, else the HIGHER of the plan's entry and a fresh
                price (under 15 minutes old)
  reward:risk   net of charges on both outcomes, under min_reward_risk
  flat_by       (MIS) the order carries no flat_by, or it is later than default_flat_by() for this
                stock, broker and day: 5 minutes before the broker's MIS square-off (earlier for
                F&O "closing auction" stocks until 2026-12-06), and never later than your flat_by
  window        (MIS) before no_entries_before, after no_entries_after, after the end
                of continuous trading in that stock (15:15 for F&O stocks until
                2026-12-06, else 15:30) minus entry_buffer_min, after
                the order's flat_by, or the market is closed (weekend, NSE
                holiday, outside 09:15-15:30). Special sessions (Muhurat):
                entries from open+15 min to close-15 min. CNC only gets a
                warning when the market is closed (the order would queue).
  daily loss    (MIS) the max losses at the stops of today's MIS entries plus
                this one, over max_daily_loss_inr (win or lose, each counts)
  trades        (MIS) max_trades_per_day MIS entries already placed today

CNC investing is not a day trade: it is not counted in trades per day or the
daily loss, and needs no stop unless you switch require_stop_cnc on.

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to nse/state/risk.json with a fingerprint of each order,
so nse-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import csv
import hashlib
import io

#@@INDIA_CORE@@

#@@FO@@

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 50, "max_order_inr": 400000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 1000, "max_daily_loss_inr": 3000, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True, "require_stop_cnc": False,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10", "entry_buffer_min": 45,
    "mis_leverage": 5, "broker": "zerodha",
    "paper": True, "blocked": [], "allow_fno": False, "allow_short": False, "charges": {},
}
PRODUCTS = ("MIS", "CNC")
ORDER_TYPES = ("LIMIT", "MARKET", "SL", "SL-M")
VALIDITIES = ("DAY", "IOC", "TTL")
VARIETIES = ("regular", "amo")
DERIV_EXCHANGES = ("NFO", "BFO", "MCX", "CDS", "BCD")
SYMBOL = re.compile(r"^[A-Z0-9&\-_]{1,20}$")
DERIV = re.compile(r"\d{2}[A-Z]{3}.*(CE|PE|FUT)$|FUT$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PAISE = 0.005
FRESH_MINUTES = 15
SEC_LIST_URL = "https://archives.nseindia.com/content/equities/sec_list.csv"
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "trigger": "the trigger price", "stale price": "an old price"}


class RiskError(IndiaError):
    pass


# ---------------------------------------------------------------- time windows

def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def hhmm(value, key):
    """"09:30" -> time(9, 30); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an India time like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def _plus(t, minutes):
    dt = datetime.datetime.combine(datetime.date(2000, 1, 1), t) + datetime.timedelta(minutes=minutes)
    return dt.time()


def window_check(now_ist, c, product, flat_by=None, cas=False):
    """(reasons, warnings): why an entry cannot be placed now (India time). `cas`: an F&O stock, whose
    continuous trading ends at 15:15 (until 2026-12-06) instead of 15:30."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    warns = []
    d, t = now_ist.date(), now_ist.time().replace(second=0, microsecond=0)
    note = calendar_note(d)
    if note:
        warns.append(note)
    mis = product == "MIS"
    if mis and before is None and after is None:
        return [], warns
    stamp = now_ist.strftime("%a %Y-%m-%d %H:%M")
    s = session(d)
    closed = None
    if s is None:
        closed = "the market is closed today (%s, %s IST)" % (trading_day(d)[1], stamp)
    elif t < s[0] or now_ist.time() >= s[1]:
        closed = "the market is closed (%s IST; the session is %s to %s)" % (stamp, hm(s[0]), hm(s[1]))
    if closed:
        if mis:
            return ["window: " + closed], warns
        warns.append("%s: a CNC order placed now would queue as an after-market order or be rejected" % closed)
        return [], warns
    if not mis:
        return [], warns
    cc = continuous_close(d, cas)
    why = ""
    if d.isoformat() in SPECIAL_SESSIONS:
        before, after = _plus(s[0], 15), _plus(cc, -15)
        warns.append("special session today (%s): entries only from %s to %s"
                     % (SPECIAL_SESSIONS[d.isoformat()]["name"], hm(before), hm(after)))
    else:
        buf = int(c.get("entry_buffer_min") if c.get("entry_buffer_min") is not None else 45)
        cutoff = _plus(cc, -buf)
        if after is None or cutoff < after:
            after = cutoff
            why = " (continuous trading in this stock ends at %s, and your entry buffer is %d minutes)" % (hm(cc), buf)
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (hm(before), hm(t))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)%s" % (hm(after), hm(t), why)], warns
    try:
        fb = hhmm(flat_by, "flat_by") if flat_by else None
    except RiskError:
        fb = None                        # the flat_by check reports a bad time
    if fb is not None and t >= fb:
        return ["window: it is %s, at or past this order's flat_by %s: it could not be closed in time"
                % (hm(t), hm(fb))], warns
    return [], warns


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


NEUTRAL_FIELDS = ("symbol", "exchange", "product", "side", "qty", "order_type", "price", "stop_trigger",
                  "target_price")


def fingerprint(order):
    """Same function in nse-budget/size.py and nse-broker/ticket.py: the exact order that was checked.
    sha1 of the broker-neutral fields, the entry leg, the stop leg (or the GTT OCO) and the target price."""
    tgt = order.get("target") or {}
    core = {"entry": order.get("entry"), "stop": order.get("stop"), "gtt_oco": order.get("gtt_oco"),
            "target": tgt.get("price"),
            "neutral": {k: order.get(k) for k in NEUTRAL_FIELDS if order.get(k) is not None} or None}
    core = {k: v for k, v in core.items() if v is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _p(x):
    return "%.2f" % float(x)


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def _paise(x):
    return int(round(float(x) * 100))


def keyof(sym, product):
    return "%s:%s" % (sym, product)


def account(cfg, portfolio, prices):
    """Starting account: cash, shares and the money tied up per (symbol, product). No portfolio = a
    fresh account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no nse/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or "").upper()
        if not t:
            continue
        prod = str(p.get("product") or "CNC").upper()
        k = keyof(t, prod)
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[k] = shares.get(k, 0) + n
        if prod == "MIS" and _f(p.get("margin")) is not None:
            values[k] = values.get(k, 0.0) + _f(p["margin"])
        elif prices.get(t):
            values[k] = shares[k] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[k] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, inr(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """nse-broker logs the kind nse-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by nse-broker."""
    return [r for r in (state or {}).get("orders", []) if r.get("status") == "placed" and rec_day(r) == today]


def parse_sec_list(text):
    """sec_list.csv text -> {SYMBOL: {"series", "band", "remarks", "name"}}. Columns have stray spaces."""
    out = {}
    rd = csv.reader(io.StringIO(text.lstrip("﻿")))
    head = None
    for row in rd:
        row = [x.strip() for x in row]
        if head is None:
            head = [h.lower() for h in row]
            continue
        if len(row) < len(head) or not row[0]:
            continue
        r = dict(zip(head, row))
        band = r.get("band", "")
        try:
            band = float(band)
        except ValueError:
            band = None            # "No Band": dynamic bands
        rem = r.get("remarks", "")
        out[r["symbol"].upper()] = {"series": r.get("series", "").upper(), "band": band,
                                    "remarks": "" if rem in ("-", "") else rem, "name": r.get("security name", "")}
    return out


def prev_close_from_chart(doc, today):
    """The last daily close BEFORE `today` (an IST date) from a Yahoo chart document, or None."""
    try:
        res = doc["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        off = int(res["meta"].get("gmtoffset", 19800))
    except (KeyError, IndexError, TypeError):
        return None
    best = None
    for t, c in zip(ts, closes):
        if c is None:
            continue
        day = (utc_of(t) + datetime.timedelta(seconds=off)).date()
        if day < today:
            best = float(c)
    return best


def band_limits(prev, band):
    """(lower, upper) circuit prices for a previous close and a band percent, on the tick grid."""
    return tick_up(prev * (1 - band / 100.0)), tick_down(prev * (1 + band / 100.0))


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          secs=None, prev_close=None, fo=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its India date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh
    secs         parse_sec_list() output; None = series / GSM / ASM / circuit are not checked (warning)
    prev_close   {SYM: previous close} for the circuit band
    fo           set of F&O stock symbols (closing-auction stocks); None = unknown, every stock is treated as one"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    prof = broker_profile(c)                 # an unknown broker is refused here (IndiaError)
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    prev_close = {str(k).upper(): float(v) for k, v in (prev_close or {}).items() if _f(v)}
    now_utc = now or parse_now(None)
    now_ist = to_ist(now_utc)
    today = today or now_ist.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    lev = max(float(c.get("mis_leverage") or 1), 1.0)
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r) and str(r.get("product") or "MIS").upper() == "MIS"]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(str(r.get("symbol", "")).upper(), str(r.get("side", "")).lower(),
                    str(r.get("product") or "MIS").upper()) for r in done_today}
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    if fo is None:
        top_warn.append("the F&O stock list was not loaded: every stock is treated as a closing-auction stock, "
                        "so the flat_by and entry cutoffs are the earlier ones")
    if secs is None:
        top_warn.append("sec_list.csv was not loaded: series, GSM / ASM and the circuit band are NOT checked")
    note = calendar_note(now_ist.date())
    if note:
        top_warn.append(note)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        e = o.get("entry") if isinstance(o.get("entry"), dict) else {}
        if not e and o.get("side") and o.get("qty") is not None:       # broker-neutral order, no rendered legs
            e = {"tradingsymbol": o.get("symbol"), "exchange": o.get("exchange"), "transaction_type": o.get("side"),
                 "quantity": o.get("qty"), "product": o.get("product"), "order_type": o.get("order_type"),
                 "price": o.get("price"), "validity": "DAY"}
        sleg = o.get("stop") if isinstance(o.get("stop"), dict) else None
        gtt = o.get("gtt_oco") if isinstance(o.get("gtt_oco"), dict) else None
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        tgt = o.get("target") if isinstance(o.get("target"), dict) else {}
        sym = str(e.get("tradingsymbol") or o.get("symbol") or "").strip().upper()
        exch = str(e.get("exchange") or o.get("exchange") or "NSE").strip().upper()
        prod = str(e.get("product") or o.get("product") or "").strip().upper()
        side = str(e.get("transaction_type", "")).strip().lower()
        typ = str(e.get("order_type", "")).strip().upper()
        validity = str(e.get("validity") or "DAY").strip().upper()
        variety = str(e.get("variety") or "regular").strip().lower()
        qty_raw = e.get("quantity")
        q = _f(qty_raw)
        value = None
        flat_by = o.get("flat_by") or plan.get("flat_by")

        # format
        if not e:
            reasons.append("format: no entry leg (nse-budget writes it)")
        if not sym:
            reasons.append("format: no tradingsymbol")
        if side not in ("buy", "sell"):
            reasons.append("format: transaction_type must be BUY or SELL, got %r" % e.get("transaction_type"))
        if prod not in PRODUCTS:
            reasons.append("format: product must be MIS (intraday) or CNC (delivery), got %r" % (prod or None))
        if q is None or q <= 0 or abs(q - round(q)) > 1e-9:
            reasons.append("format: quantity must be a whole number of shares, got %r" % qty_raw)
            q = None
        if typ not in ORDER_TYPES:
            reasons.append("format: order_type must be one of %s, got %r" % (", ".join(ORDER_TYPES), e.get("order_type")))
        if validity not in VALIDITIES:
            reasons.append("format: validity must be DAY, IOC or TTL, got %r" % e.get("validity"))
        if variety not in VARIETIES:
            warns.append("variety %r is not regular or amo: check every field before you say yes" % variety)
        for k in ("price", "trigger_price"):
            if e.get(k) is not None:
                v = _f(e.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, e.get(k)))
                elif abs(round(v / 0.05) * 0.05 - v) > 1e-6:
                    warns.append("%s %s is not a multiple of 0.05: most NSE stocks tick in 0.05" % (k, e.get(k)))
        if typ in ("LIMIT", "SL") and e.get("price") is None:
            reasons.append("format: a %s order needs a price" % typ)
        if typ in ("SL", "SL-M") and e.get("trigger_price") is None:
            reasons.append("format: a %s order needs a trigger_price" % typ)
        if sleg is not None:
            sq = _f(sleg.get("quantity"))
            if str(sleg.get("transaction_type", "")).upper() == str(e.get("transaction_type", "")).upper():
                reasons.append("format: the stop leg must be on the other side of the entry")
            if q is not None and sq != q:
                reasons.append("format: the stop leg quantity %r does not match the entry's %r" % (sleg.get("quantity"), qty_raw))
            if str(sleg.get("product", "")).upper() != prod:
                reasons.append("format: the stop leg product %r does not match the entry's %s" % (sleg.get("product"), prod))
            if str(sleg.get("order_type", "")).upper() not in ("SL-M", "SL"):
                reasons.append("format: the stop leg must be an SL-M (or SL) order")
            if _f(sleg.get("trigger_price")) is None or _f(sleg.get("trigger_price")) <= 0:
                reasons.append("format: the stop leg needs a positive trigger_price")
            if str(sleg.get("tradingsymbol", "")).upper() != sym:
                reasons.append("format: the stop leg is for %r, the entry for %s" % (sleg.get("tradingsymbol"), sym))
        if gtt is not None:
            tv = gtt.get("trigger_values")
            go = gtt.get("orders")
            if gtt.get("trigger_type") != "two-leg" or not isinstance(tv, list) or len(tv) != 2 \
                    or _f(tv[0]) is None or _f(tv[1]) is None or not (0 < _f(tv[0]) < _f(tv[1])):
                reasons.append("format: a GTT OCO needs trigger_type \"two-leg\" and trigger_values [stop, target] "
                               "with stop below target")
            if not isinstance(go, list) or len(go) != 2:
                reasons.append("format: a GTT OCO needs exactly two orders (sell at the stop, sell at the target)")
            else:
                for g in go:
                    if str(g.get("transaction_type", "")).upper() != "SELL" or (q is not None and _f(g.get("quantity")) != q):
                        reasons.append("format: each GTT OCO order must be a SELL for the entry's quantity")
                        break
            if prod == "MIS":
                reasons.append("format: a GTT cannot protect an MIS position (GTT is for delivery); use the SL-M stop")

        # asset
        if exch in DERIV_EXCHANGES or (sym and DERIV.search(sym)):
            if not c.get("allow_fno"):
                reasons.append("asset: %s looks like a derivatives contract; this pack trades NSE / BSE cash "
                               "equity only (\"allow_fno\": false)" % sym)
            else:
                warns.append("derivatives order: this pack's risk maths does not model F&O margin or expiry")
        elif exch not in ("NSE", "BSE"):
            reasons.append("asset: exchange %r is not NSE or BSE" % exch)
        elif sym and not SYMBOL.match(sym):
            reasons.append("asset: %s does not look like an NSE / BSE equity symbol" % sym)

        # the instrument: series, GSM / ASM, band
        info_sec = (secs or {}).get(sym) if (secs is not None and exch == "NSE") else None
        series = (info_sec or {}).get("series") or str(o.get("series") or "EQ").upper()
        if secs is not None and exch == "NSE" and sym and info_sec is None and SYMBOL.match(sym):
            warns.append("%s is not in sec_list.csv (all NSE series with a price band): check the symbol" % sym)
        elif secs is not None and exch == "BSE":
            warns.append("BSE order: the NSE band list does not cover BSE price bands; series, GSM / ASM and "
                         "circuit are not checked")

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        psym = sym + ".BO" if exch == "BSE" else sym      # the key in prices.json
        live = fresh(psym)
        ref, ref_from = None, None
        if typ in ("LIMIT", "SL"):
            ref, ref_from = _f(e.get("price")), "limit"
        elif typ == "SL-M":
            ref, ref_from = _f(e.get("trigger_price")), "trigger"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = r2(ref)
                if live and ref_from == "price" and plan_entry and live > plan_entry + PAISE:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), _p(plan_entry), _p(ref)))
            elif psym in prices:
                ref, ref_from = prices[psym], "stale price"
        if plan_entry and live is None and psym in prices and price_times is not None and typ == "MARKET":
            warns.append("the %s price in nse/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        if q and ref:
            value = q * ref

        # entry or exit
        k = keyof(sym, prod)
        held = acct["shares"].get(k, 0.0)
        held_value = acct["values"].get(k, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9 and q is not None:
            exiting = q <= held + 1e-9
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        mis = prod == "MIS"
        day_rules = mis and kind == "entry"

        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %d-share order; add it to nse/prices.json "
                         "(nse-alerts: alerts.py quote %s --write)" % (sym, int(q), sym))
        if kind == "entry" and side == "buy" and prod == "MIS" and series in ("BE", "BZ"):
            later.append("series: %s trades in series %s (trade-for-trade): delivery only, MIS is not allowed"
                         % (sym, series))
        if info_sec and (re.search(r"\b(GSM|ASM)\b", info_sec["remarks"], re.I)):
            if mis:
                later.append("gsm/asm: %s is under surveillance (%s): MIS is blocked, delivery only"
                             % (sym, info_sec["remarks"]))
            else:
                warns.append("%s is under surveillance (%s): higher margins, trade limits and price-band "
                             "changes can follow; read the exchange notice" % (sym, info_sec["remarks"]))
        elif info_sec and info_sec["remarks"]:
            warns.append("%s sec_list remark: %s" % (sym, info_sec["remarks"]))
        if series in ("SM", "ST"):
            warns.append("%s is an SME stock (series %s): thin trading and big lots; many brokers refuse MIS" % (sym, series))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < float(c.get("min_order_inr") or 0) - PAISE:
                later.append("size: %s is under your %s minimum order" % (inr(value), inr(c["min_order_inr"], 0)))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + PAISE:
                later.append("size: %s is over your %s max order" % (inr(value), inr(c["max_order_inr"], 0)))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        tied = None
        if value is not None:
            tied = value / lev if mis else value
        if tied is not None and side == "buy" and kind == "entry":
            after_cash = cash - tied
            what = "margin (%s / %sx leverage)" % (inr(value), _num(lev)) if mis else "cash"
            if after_cash < -PAISE:
                later.append("cash: needs %s %s but only %s cash" % (inr(tied), what, inr(cash)))
            elif after_cash < buffer_inr - PAISE:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (inr(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), inr(buffer_inr)))
            pos_after = sum(v for kk, v in acct["values"].items() if kk.split(":")[0] == sym) + tied
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + PAISE:
                later.append("position: %s would tie up %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, inr(pos_after), 100.0 * pos_after / total, inr(total), _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            over = (q > held + 1e-9) if q is not None else held <= 0
            if over:
                if prod == "CNC":
                    reasons.append("short: selling more %s than you hold (%s shares); CNC cannot be sold short, "
                                   "you can only sell delivery shares that are in your demat. If you bought it, "
                                   "record the fill first (ticket.py fill)" % (sym, _num(held)))
                elif not c.get("allow_short"):
                    reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                                   "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # trade checks: stop, loss, reward:risk, flat_by, window, daily loss, trades
        max_loss = risk_ps = rr = stop = target = charges_stop = charges_tgt = None
        if kind == "entry" and side in ("buy", "sell") and prod in PRODUCTS:
            long = side == "buy"
            leg_stop = (_f(sleg.get("trigger_price")) if sleg else None) or _f(o.get("stop_trigger"))
            plan_stop = _f(plan.get("stop"))
            gtt_stop = gtt_tgt = None
            if gtt and isinstance(gtt.get("trigger_values"), list) and len(gtt["trigger_values"]) == 2:
                gtt_stop, gtt_tgt = _f(gtt["trigger_values"][0]), _f(gtt["trigger_values"][1])
            stop = leg_stop or gtt_stop or plan_stop
            target = _f(tgt.get("price")) or _f(o.get("target_price")) or gtt_tgt or _f(plan.get("target"))
            at_broker = bool(leg_stop or gtt_stop)
            if at_broker and plan_stop and abs((leg_stop or gtt_stop) - plan_stop) > PAISE:
                warns.append("the broker stop %s differs from the plan's stop %s: the order's is the one "
                             "that is placed" % (_p(leg_stop or gtt_stop), _p(plan_stop)))
            if stop and not at_broker:
                warns.append("the stop %s is in the plan only, not an order: set a stop alert "
                             "(nse-alerts: alerts.py add --from-plan)" % _p(stop))
            need_stop = c.get("require_stop", True) if mis else c.get("require_stop_cnc", False)
            if not stop:
                if need_stop:
                    later.append("stop: no stop. Every %s entry needs one (\"%s\": true): %s"
                                 % (prod, "require_stop" if mis else "require_stop_cnc",
                                    "an SL-M stop leg from nse-exit and nse-budget" if mis
                                    else "a GTT OCO from nse-exit and nse-budget"))
                else:
                    warns.append("no stop: the loss is not capped by a stop, so it is not counted in the "
                                 "daily loss%s" % ("" if mis else " (CNC delivery buy: GTT stop optional)"))
            elif ref:
                if (long and stop >= ref - PAISE) or (not long and stop <= ref + PAISE):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_ps = round(abs(ref - stop), 2)
                    shares = q if q is not None else 0
                    xchg = "BSE" if exch == "BSE" else "NSE"
                    if long:
                        ch_s = trade_charges(shares * ref, shares * stop, prod, xchg, c)["total"]
                    else:
                        ch_s = trade_charges(shares * stop, shares * ref, prod, xchg, c)["total"]
                    charges_stop = ch_s
                    max_loss = round(shares * risk_ps + ch_s, 2)
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and max_loss > cap + PAISE:
                        later.append("loss: %d shares x %s risk a share = %s, plus %s charges = %s at the stop, "
                                     "over your %s max loss per trade"
                                     % (shares, _p(risk_ps), inr(shares * risk_ps), inr(ch_s), inr(max_loss),
                                        inr(cap, 0)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + PAISE) or (not long and target >= ref - PAISE):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            if long:
                                charges_tgt = trade_charges(shares * ref, shares * target, prod, xchg, c)["total"]
                            else:
                                charges_tgt = trade_charges(shares * target, shares * ref, prod, xchg, c)["total"]
                            net = shares * abs(target - ref) - charges_tgt
                            rr = round(net / max_loss, 2) if max_loss > 0 else 0.0
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f after charges (target %s, entry %s, stop %s) is "
                                             "under your minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr and mis:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)" % _num(min_rr))
            # circuit band: entry, stop and target must all be tradable prices
            pc = prev_close.get(psym) or _f(o.get("prev_close"))
            if info_sec is not None:
                band = info_sec["band"]
                if band is None:
                    warns.append("%s has no fixed price band (dynamic bands apply): the circuit check is skipped" % sym)
                elif not pc:
                    warns.append("no previous close for %s: the %s%% circuit band was not checked" % (sym, _pct(band)))
                else:
                    lo, hi = band_limits(pc, band)
                    for nm, px in (("entry", ref), ("stop", stop), ("target", target)):
                        if px and px < lo - PAISE:
                            later.append("circuit: the %s %s is below the lower circuit %s (previous close %s, %s%% "
                                         "band): it cannot trade there" % (nm, _p(px), _p(lo), _p(pc), _pct(band)))
                        elif px and px > hi + PAISE:
                            later.append("circuit: the %s %s is above the upper circuit %s (previous close %s, %s%% "
                                         "band): it cannot trade there" % (nm, _p(px), _p(hi), _p(pc), _pct(band)))
                    info.append("circuit band %s to %s (previous close %s, %s%%)" % (_p(lo), _p(hi), _p(pc), _pct(band)))
            # MIS: flat_by, window, daily loss, trades
            cas = is_cas_stock(fo, sym)
            if mis:
                latest_fb, so, so_note = default_flat_by(now_ist.date(), cas, c)
                if so_note and so_note not in top_warn:
                    top_warn.append(so_note)
                info.append("%s MIS square-off %s%s: flat_by must be at or before %s"
                            % (prof["name"], hm(so), " (F&O closing-auction stock)" if cas else "", hm(latest_fb)))
                if not flat_by:
                    later.append("flat_by: an MIS order must carry a flat_by time at or before %s (nse-budget writes it)"
                                 % hm(latest_fb))
                else:
                    try:
                        fb = hhmm(flat_by, "flat_by")
                        if fb > latest_fb:
                            later.append("flat_by: %s is too late; it must be at or before %s (%s squares off MIS at %s "
                                         "for this stock, 5 minutes of margin, and never later than your flat_by)"
                                         % (hm(fb), hm(latest_fb), prof["name"], hm(so)))
                    except RiskError as ex:
                        later.append("flat_by: %s" % ex)
            w_reasons, w_warns = window_check(now_ist, c, prod, flat_by, cas)
            later.extend(w_reasons)
            warns.extend(x for x in w_warns if x not in top_warn)
            daily = _f(c.get("max_daily_loss_inr"))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if day_rules:
                if daily and max_loss is not None:
                    used = loss_today + batch_loss
                    if used + max_loss > daily + PAISE:
                        later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                     "daily loss limit" % (inr(used), inr(max_loss), inr(used + max_loss), inr(daily, 0)))
                if cap_t and len(entries_today) + batch_entries >= cap_t:
                    later.append("trades: %d MIS entries placed today plus %d passed earlier in this batch, your limit "
                                 "is %d a day" % (len(entries_today), batch_entries, cap_t))
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s = %s + charges %s%s, reward:risk %s after charges%s"
                            % (inr(max_loss), inr((q or 0) * risk_ps), inr(charges_stop),
                               (" (limit %s)" % inr(c["max_loss_per_trade_inr"], 0))
                               if _f(c.get("max_loss_per_trade_inr")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if mis and tied is not None:
                info.append("order value %s, margin %s at %sx leverage" % (inr(value), inr(tied), _num(lev)))
            if day_rules and (daily or cap_t):
                info.append("today before this: %d of %s MIS trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               inr(loss_today + batch_loss), inr(daily, 0) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side, prod)
        if key in seen:
            later.append("duplicate: a second %s %s (%s) in this batch" % (side, sym, prod))
        if key in placed_keys:
            later.append("duplicate: a %s %s (%s) was already placed today" % (side, sym, prod))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if day_rules and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "buy" and q is not None:
                if tied is not None:
                    acct["cash"] -= tied
                    acct["values"][k] = acct["values"].get(k, 0.0) + tied
                acct["shares"][k] = held + q
            elif side == "sell" and q is not None:
                frac = min(q / held, 1.0) if held > 1e-9 else 0.0
                released = held_value * frac
                acct["cash"] += (value if (value is not None and not mis) else released)
                acct["values"][k] = max(held_value - released, 0.0)
                acct["shares"][k] = held - q
        results.append({
            "index": i, "symbol": sym, "exchange": exch, "product": prod, "side": side, "kind": kind,
            "amount": ("%s shares" % qty_raw), "series": series,
            "value_inr": round(value, 2) if value is not None else None,
            "margin_inr": round(tied, 2) if tied is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "charges_at_stop": charges_stop, "charges_at_target": charges_tgt,
            "reward_risk": rr, "flat_by": flat_by if mis else None, "cas_stock": is_cas_stock(fo, sym),
            "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now_ist.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "mis_entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                   r["product"], r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_ist"], inr(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. nse-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}, {SYM: previous close}).

    Keys may be RELIANCE or RELIANCE.NS; both are stored as RELIANCE. The price time is the quote's own
    time (detail.time), else when it was fetched (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}, {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}, {}
    out, times, prevs = {}, {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = {str(k).upper(): v for k, v in (pdoc.get("detail") or {}).items()}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        raw = str(s).upper()
        s2 = re.sub(r"\.NS$", "", raw)       # BSE stays SYMBOL.BO, as nse-alerts writes it
        out[s2] = float(p)
        d = detail.get(raw) or detail.get(s2) or {}
        times[s2] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
        if _f(d.get("prev_close")):
            prevs[s2] = float(d["prev_close"])
    return out, times, prevs


def get_sec_list(path, cache, offline, today):
    """(secs or None, note). --seclist first, then the day's cache, then a download (not offline)."""
    if path:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse_sec_list(fh.read()), None
    if cache and os.path.exists(cache):
        age = time.time() - os.path.getmtime(cache)
        if offline or age < 20 * 3600:
            with open(cache, encoding="utf-8", errors="replace") as fh:
                return parse_sec_list(fh.read()), None
    if offline:
        return None, "offline and no sec_list.csv given (--seclist) or cached"
    try:
        text = _open_url(SEC_LIST_URL, accept="text/csv")
    except (urllib.error.URLError, OSError) as e:
        return None, "could not download sec_list.csv (%s)" % e
    if cache:
        d = os.path.dirname(cache)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            fh.write(text)
    return parse_sec_list(text), None


def fetch_prev_close(yahoo, today):
    try:
        doc = json.loads(yahoo_get(chart_path(yahoo, "5d", "1d")))
    except (IndiaError, ValueError):
        return None
    return prev_close_from_chart(doc, today)


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (nse-risk).")
    ap.add_argument("--orders", default="nse/orders.json")
    ap.add_argument("--config", default="nse/config.json")
    ap.add_argument("--portfolio", default="nse/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="nse/prices.json")
    ap.add_argument("--state", default="nse/state/broker.json", help="what nse-broker placed today")
    ap.add_argument("--seclist", help="a local sec_list.csv (default: download it, cached in nse/state/)")
    ap.add_argument("--offline", action="store_true", help="no network: sec_list from --seclist or the cache, "
                                                            "previous close from prices.json or the order only")
    ap.add_argument("--fo-lots", help="a local fo_mktlots.csv (default: cached/downloaded as "
                                      "nse/state/fo_mktlots-YYYY-MM-DD.csv)")
    ap.add_argument("--now", help="pretend it is this India time (IST), e.g. \"2026-10-26 09:45\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in India)")
    ap.add_argument("--out", default="nse/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        load_calendar()
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-26")
        doc = read_json(a.orders, "orders file (run nse-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times, prevs = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        today_d = datetime.date.fromisoformat(a.today) if a.today else to_ist(now).date()
        cache = os.path.join(os.path.dirname(a.out) or ".", "sec_list.csv") if not a.no_write else None
        secs, sec_note = get_sec_list(a.seclist, cache, a.offline, today_d)
        state_dir = os.path.dirname(a.out) or "nse/state"
        fo, fo_note = load_fo(state_dir, today_d, a.offline, a.fo_lots, write_cache=not a.no_write)
        for o in orders:
            e = o.get("entry") or {}
            s = str(e.get("tradingsymbol") or o.get("symbol") or "").upper()
            if s and s not in prevs and not _f(o.get("prev_close")) and not a.offline and secs is not None \
                    and (secs.get(s) or {}).get("band") is not None:
                y = o.get("yahoo") or (s + ".NS")
                pc = fetch_prev_close(y, today_d)
                if pc:
                    prevs[s] = pc
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, secs, prevs, fo)
        if sec_note:
            res["warnings"].append(sec_note)
        if fo_note:
            res["warnings"].append(fo_note)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = iso(utc_now())
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, a.out)
    except IndiaError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
