#!/usr/bin/env python3
"""Position sizing for nse-budget. Standard library only, no network.

Default mode: sizes ONE trade from nse/plan.json (written by nse-exit) to your
budget and your max loss per trade, then writes one order set to
nse/orders.json in the nse-budget order contract (Zerodha Kite place_order
fields). It never places anything: nse-risk checks the order and nse-broker
asks you yes or no.

    python3 size.py                                  # nse/plan.json + nse/config.json
    python3 size.py --write                          # write nse/orders.json
    python3 size.py --product CNC --write            # a delivery (CNC) order with a GTT OCO exit
    python3 size.py --limit                          # a limit order exactly at the plan's entry
    python3 size.py --limit 1196.10 --write          # a limit order at 1196.10
    python3 size.py --now "2026-10-26 09:45" --json  # pin the clock (India time, IST)

How many shares. Whole shares only. Every limit below is a number of shares;
the smallest wins, and the report says which one decided it.

    max loss       max_loss_per_trade_inr: (entry - stop) x shares PLUS the
                   round-trip charges if the stop fills (STT, brokerage,
                   exchange fees, GST, stamp duty, and for CNC the DP charge)
    position cap   max_position_pct of the budget, measured on the money you
                   tie up (the margin for MIS, the full price for CNC)
    max order      max_order_inr, measured on the order value (shares x price)
    cash/margin    (budget minus the cash buffer) against the margin needed:
                   MIS needs value / mis_leverage (default 5), CNC needs the
                   full value. Never more than the cash in nse/portfolio.json.

The entry it sizes at: a LIMIT order, at the plan's entry rounded UP to a tick,
or at the price in nse/prices.json if that is fresh (under 15 minutes old) and
higher, because a price that ran up since the plan means a bigger loss at the
same stop. --limit PRICE sets it yourself. The stop rounds DOWN to a tick and
so does the target. Zero shares (the stop is too far for your max loss) is a
plain refusal: no order is written. So is a net reward:risk under
min_reward_risk (the price ran toward the target, or charges eat the reward).

Allocation mode (kept from the US pack, for CNC portfolios): split a budget
across a list of stocks by weight, in rupees and whole shares.

    python3 size.py --weights RELIANCE=30,TCS=25,HDFCBANK=25,INFY=20 --prices nse/prices.json
    python3 size.py --targets my-weights.json --budget 500000 --write

  1. cash buffer   budget x cash_buffer_pct stays in cash, never invested
  2. weights       renormalised to 100 over the stocks you keep (blocked
                   tickers are dropped first; --top N keeps the N biggest)
  3. cap           no stock gets more than max_position_pct of the budget;
                   anything over the cap is shared out to the others by
                   weight, again and again until nobody is over
  4. min order     a slice under min_order_inr is dropped and its weight
                   shared out, so you never place a tiny order
  5. whole shares  NSE has no fractional shares: each slice is floored to
                   whole shares, then one more share at a time goes to the
                   stock furthest below its slice while cash lasts

Money that cannot be invested is reported as "left idle", never hidden.
"""

import argparse
import hashlib
import math

#@@INDIA_CORE@@

#@@FO@@

DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 50,
    "max_order_inr": 400000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 1000,
    "max_daily_loss_inr": 3000,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "require_stop_cnc": False,
    "no_entries_before": "09:30",
    "no_entries_after": "14:45",
    "flat_by": "15:10",
    "entry_buffer_min": 45,
    "mis_leverage": 5,
    "product": "MIS",
    "broker": "zerodha",
    "paper": True,
    "blocked": [],
    "allow_fno": False,
    "allow_short": False,
}
EPS = 1e-9
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}
PRODUCT_WORDS = {"MIS": "MIS, intraday", "CNC": "CNC, delivery"}


class SizeError(IndiaError):
    pass


# ---------------------------------------------------------------- inputs

def parse_utc(text):
    """"2026-10-26T04:12:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_weights(data):
    """Accept a weights file, a {symbol: weight} dict or a list of rows.

    Returns [{"ticker", "name", "weight"}] with weight as a float."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"ticker": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("ticker") or r.get("symbol")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a ticker and a weight" % i)
            rows.append({"ticker": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["ticker"], r["weight"]))
        out.append({"ticker": plain_symbol(r["ticker"]), "name": r["name"] or "", "weight": w})
    return out


def plain_symbol(text):
    """RELIANCE.NS / nse:reliance / RELIANCE -> RELIANCE (the tradingsymbol)."""
    return parse_symbol(text)["tradingsymbol"]


def weights_from_arg(text):
    """RELIANCE=30,TCS=25 -> {symbol: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants SYMBOL=WEIGHT pairs, got %r" % part)
        t, w = part.split("=", 1)
        out[t.strip().upper()] = w.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in prices.items():
        try:
            out[str(t).upper()] = float(p)
        except (TypeError, ValueError):
            continue
    return out


def _lookup(table, sym, yahoo=None):
    # nse-alerts keys an NSE stock by its symbol and a BSE stock by SYMBOL.BO
    bse = bool(yahoo and str(yahoo).upper().endswith(".BO"))
    order = ((sym or "") + ".BO", yahoo) if bse else (sym, yahoo, (sym or "") + ".NS")
    for k in order:
        if k and k in table:
            return k
    return None


def price_time(doc, key):
    """When this symbol's price is from: the quote's own time (detail[key].time), else when it
    was fetched (detail[key].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(key) or {}
    for k in ("time", "fetched"):
        t = parse_utc(det.get(k))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, yahoo=None, minutes=FRESH_MINUTES):
    """(price, fetched_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    table = {str(k).upper(): v for k, v in (doc.get("prices") or {}).items()}
    key = _lookup(table, sym, yahoo)
    if key is None:
        return None, None, False
    p = _f(table[key])
    if p is None or p <= 0:
        return None, None, False
    detail = {str(k).upper(): v for k, v in (doc.get("detail") or {}).items()}
    t = price_time({"detail": detail, "as_of": doc.get("as_of")}, key)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- order contract

ORDER_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
                "order_type", "price", "trigger_price", "validity")


NEUTRAL_FIELDS = ("symbol", "exchange", "product", "side", "qty", "order_type", "price", "stop_trigger",
                  "target_price")


def fingerprint(order):
    """Same function in nse-risk/check.py and nse-broker/ticket.py: the exact order that was checked.
    sha1 of the broker-neutral fields, the entry leg, the stop leg (or the GTT OCO) and the target price."""
    tgt = order.get("target") or {}
    core = {"entry": order.get("entry"), "stop": order.get("stop"), "gtt_oco": order.get("gtt_oco"),
            "target": tgt.get("price"),
            "neutral": {k: order.get(k) for k in NEUTRAL_FIELDS if order.get(k) is not None} or None}
    core = {k: v for k, v in core.items() if v is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _num(x):
    """1250 -> "1250", 1118.13 -> "1118.13"."""
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def _price(x):
    return "%.2f" % round(float(x), 2)


def shares_for(amount, per_share):
    """Whole shares that fit in `amount` at `per_share` (a tiny epsilon for float noise)."""
    if per_share <= 0 or amount <= 0:
        return 0
    return int(math.floor(amount / per_share + EPS))


# ---------------------------------------------------------------- plan mode (the default)

def round_trip(qty, entry, exit_price, product, exchange, cfg):
    """Charges on buying qty at entry and selling at exit_price."""
    return trade_charges(qty * entry, qty * exit_price, product, exchange, cfg)


def loss_at(qty, entry, stop, product, exchange, cfg):
    """(max loss, charges total): the rupees lost if the stop fills at the stop, charges included."""
    ch = round_trip(qty, entry, stop, product, exchange, cfg)["total"]
    return round(qty * (entry - stop) + ch, 2), ch


def max_loss_shares(max_loss, entry, stop, product, exchange, cfg):
    """The most whole shares whose loss at the stop, charges included, fits max_loss."""
    risk = entry - stop
    hi = int(math.floor(max_loss / risk + EPS)) if risk > 0 else 0
    lo = 0
    while lo < hi:                       # loss grows with shares: binary search the biggest that fits
        mid = (lo + hi + 1) // 2
        if loss_at(mid, entry, stop, product, exchange, cfg)[0] <= max_loss + 0.005:
            lo = mid
        else:
            hi = mid - 1
    return lo


def parse_entry_time(text):
    """"09:45", "9:45 AM" -> "0945" for the order id, or None."""
    if not text:
        return None
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        h = h % 12 + (12 if m.group(3).lower() == "pm" else 0)
    return "%02d%02d" % (h, mi)


def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None, product=None,
               cas_stock=True):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit    the limit price (None = the plan's entry, or a higher fresh price)
    price    a FRESH latest price; the order is sized at the higher of it and the plan's entry
    cash     the cash in nse/portfolio.json, if there is one
    today    India date (YYYY-MM-DD); a plan for another day is refused
    product  MIS or CNC (default: the plan's product, else the config's)
    cas_stock  an F&O stock (closing-auction stock): its continuous trading ends earlier, so does the
               broker's MIS square-off, so flat_by comes earlier. Unknown = True (the cautious side)"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    broker_profile(c)                        # an unknown broker is refused here (IndiaError)
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    raw = str(plan.get("symbol") or "").strip()
    if not raw:
        raise SizeError("the plan has no symbol (run nse-exit)")
    sp = parse_symbol(plan.get("yahoo") or raw, plan.get("exchange") or "NSE")
    if sp["index"]:
        raise SizeError("%s is an index: you cannot buy shares of it (try a Nifty ETF like NIFTYBEES)" % raw)
    sym, exchange = sp["tradingsymbol"], sp["exchange"]
    yahoo = sp["yahoo"]
    series = str(plan.get("series") or "EQ").upper()
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) trades only; the plan says %r" % side)
    prod = str(product or plan.get("product") or c.get("product") or "MIS").upper()
    if prod not in ("MIS", "CNC"):
        raise SizeError("product is MIS (intraday) or CNC (delivery), not %r" % prod)
    if prod == "MIS" and series in ("BE", "BZ"):
        raise SizeError("%s trades in series %s (trade-for-trade): delivery only, no MIS. "
                        "Use --product CNC or skip it" % (sym, series))
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in India: make a new plan with nse-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run nse-exit); the exit orders need a stop and a "
                            "target" % name)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in nse/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    lev = float(c.get("mis_leverage") or 1) if prod == "MIS" else 1.0
    if lev < 1:
        raise SizeError("mis_leverage must be 1 or more")

    if limit is not None:
        ref, ref_from = tick_up(limit), "limit"
    elif price is not None and tick_up(price) > tick_up(entry_plan):
        ref, ref_from = tick_up(price), "price"
    else:
        ref, ref_from = tick_up(entry_plan), "plan"
    stop, target = tick_down(stop), tick_down(target)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with nse-exit" % (_price(target), _price(ref)))
    risk = round(ref - stop, 2)
    reward = round(target - ref, 2)

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (inr(budget, 0), inr(cash_buffer, 0))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in nse/portfolio.json minus the %s buffer" % (inr(cash, 0), inr(cash_buffer, 0))
    available = max(round(available, 2), 0.0)
    margin_per_share = ref / lev

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if max_loss:
        n = max_loss_shares(max_loss, ref, stop, prod, exchange, c)
        limits.append({"name": "max loss", "inr": max_loss, "per": risk, "shares": n,
                       "how": "%s max loss, risk %s a share plus charges" % (inr(max_loss, 0), _price(risk))})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "inr": cap, "per": margin_per_share,
                       "shares": shares_for(cap, margin_per_share),
                       "how": "%s%% of %s = %s / %s%s" % (_num(cap_pct), inr(budget, 0), inr(cap, 0),
                                                           _price(margin_per_share),
                                                           " margin a share" if lev > 1 else "")})
    max_order = _f(c.get("max_order_inr"))
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (inr(max_order, 0), _price(ref))})
    limits.append({"name": "cash", "inr": available, "per": margin_per_share,
                   "shares": shares_for(available, margin_per_share),
                   "how": "%s = %s / %s%s" % (cash_note, inr(available, 0), _price(margin_per_share),
                                              " margin a share" if lev > 1 else "")})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    value = round(qty * ref, 2)
    margin = round(value / lev, 2)
    if qty > 0:
        loss, ch_stop = loss_at(qty, ref, stop, prod, exchange, c)
        ch_target = round_trip(qty, ref, target, prod, exchange, c)["total"]
        breakdown = round_trip(qty, ref, stop, prod, exchange, c)
        net_reward = qty * reward - ch_target
        rr = round(net_reward / loss, 2) if loss > 0 else 0.0
    else:
        loss = ch_stop = ch_target = 0.0
        breakdown = {}
        rr = 0.0
        one_loss, one_ch = loss_at(1, ref, stop, prod, exchange, c)
        net1 = reward - round_trip(1, ref, target, prod, exchange, c)["total"]
        rr = round(net1 / one_loss, 2) if one_loss > 0 else 0.0
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share risks %s (entry %s minus stop %s) plus %s charges, %s in all, more than your %s "
                       "max loss per trade. No order. Skip this trade, or plan a closer stop with nse-exit."
                       % (_price(risk), _price(ref), _price(stop), inr(one_ch), inr(one_loss), inr(max_loss, 0)))
        else:
            refusal = ("one share of %s needs %s (%s), more than your %s limit (%s). No order."
                       % (sym, inr(margin_per_share), "margin" if lev > 1 else "cash", bound[0],
                          limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and rr < _f(c["min_reward_risk"]) - EPS:
        refusal = ("reward:risk %.2f after charges (target %s, entry %s, stop %s) is under your minimum %s, so "
                   "nse-risk would block it. No order. Skip it, or make a new plan with nse-exit."
                   % (rr, _price(target), _price(ref), _price(stop), _num(c["min_reward_risk"])))
    elif value < float(c.get("min_order_inr") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, inr(value), inr(c["min_order_inr"], 0)))
    flat_by = squareoff = squareoff_note = None
    if prod == "MIS":
        day = datetime.date.fromisoformat(str(plan.get("date") or today or to_ist(utc_now()).date().isoformat()))
        dfb, so, squareoff_note = default_flat_by(day, cas_stock, c)
        if plan.get("flat_by"):
            dfb = min(dfb, parse_hhmm(plan["flat_by"], "the plan's flat_by"))
        flat_by, squareoff = hm(dfb), hm(so)
    return {
        "symbol": sym, "exchange": exchange, "yahoo": yahoo, "series": series, "product": prod, "side": "buy",
        "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": r2(entry_plan),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_per_share": reward, "reward_risk": rr,
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "leverage": lev, "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "value": value, "margin_needed": margin, "max_loss": loss,
        "charges_at_stop": ch_stop, "charges_at_target": ch_target, "charges_breakdown": breakdown,
        "flat_by": flat_by, "squareoff": squareoff, "squareoff_note": squareoff_note, "cas_stock": bool(cas_stock),
        "refusal": refusal, "broker": broker_profile(c)["key"], "broker_name": broker_profile(c)["name"],
    }


def build_order(s, now_ist=None, last_price=None):
    """The proposed order set in the order contract, or None on a refusal."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    q, sym, ex, prod = int(s["qty"]), s["symbol"], s["exchange"], s["product"]
    stamp = parse_entry_time(s["entry_time"]) or (now_ist.strftime("%H%M") if now_ist else "0000")
    day = (s["date"] or (now_ist.date().isoformat() if now_ist else "")).replace("-", "")
    entry = {"variety": "regular", "exchange": ex, "tradingsymbol": sym, "transaction_type": "BUY",
             "quantity": q, "product": prod, "order_type": "LIMIT", "price": s["entry"], "validity": "DAY"}
    stop_leg, gtt = None, None
    if prod == "MIS":
        stop_leg = {"variety": "regular", "exchange": ex, "tradingsymbol": sym, "transaction_type": "SELL",
                    "quantity": q, "product": prod, "order_type": "SL-M", "trigger_price": s["stop"],
                    "validity": "DAY"}
        target = {"price": s["target"], "how": "LIMIT sell placed after the fill (optional, see nse-broker), "
                                               "or an nse-alerts target alert"}
    else:
        target = {"price": s["target"], "how": "the second leg of the GTT OCO"}
        sell = {"exchange": ex, "tradingsymbol": sym, "transaction_type": "SELL", "quantity": q,
                "order_type": "LIMIT", "product": "CNC"}
        gtt = {"trigger_type": "two-leg", "tradingsymbol": sym, "exchange": ex,
               "trigger_values": [s["stop"], s["target"]], "last_price": r2(last_price or s["entry"]),
               "orders": [dict(sell, price=s["stop"]), dict(sell, price=s["target"])]}
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "trade")
    why = ("%s on %s: %d shares, bound by your %s (%d x %s + %s charges = %s at the stop)"
           % (setup, sym, q, {"max loss": "%s max loss per trade" % inr(s["max_loss_per_trade_inr"] or 0, 0),
                              "position cap": "position cap", "max order": "max order",
                              "cash": "cash after the buffer"}[s["bound_by"]],
              q, _price(s["risk_per_share"]), inr(s["charges_at_stop"]), inr(s["max_loss"])))
    o = {
        "id": "%s-%s-%s" % (sym, day, stamp),
        "symbol": sym, "exchange": ex, "yahoo": s["yahoo"], "series": s["series"], "product": prod,
        # broker-neutral order: every broker profile renders its own payload from these
        "side": "BUY", "qty": q, "order_type": "LIMIT", "price": s["entry"],
        "stop_trigger": s["stop"], "target_price": s["target"], "broker": s["broker"],
        # the Kite-shaped legs (the "zerodha" rendering)
        "entry": entry, "stop": stop_leg, "target": target, "gtt_oco": gtt,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "r_per_share": s["risk_per_share"], "setup": s["setup"], "entry_time": s["entry_time"],
                 "date": s["date"]},
        "max_loss": s["max_loss"], "charges_at_stop": s["charges_at_stop"],
        "charges_at_target": s["charges_at_target"], "reward_risk": s["reward_risk"],
        "margin_needed": s["margin_needed"], "flat_by": s["flat_by"], "why": why,
    }
    o["fingerprint"] = fingerprint(o)
    return o


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy (%s), %s, plan entry %s%s" % (
        s["symbol"], PRODUCT_WORDS[s["product"]], setup, _price(s["entry_plan"]),
        (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f after charges."
                 % (inr(s["entry"]), src, inr(s["risk_per_share"]), _price(s["entry"]), _price(s["stop"]),
                    s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-62s %5d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s order value%s."
                 % (s["qty"], s["symbol"], inr(s["value"]),
                    (", %s margin at %sx leverage (check your broker's actual margin)"
                     % (inr(s["margin_needed"]), _num(s["leverage"]))) if s["product"] == "MIS"
                    else " (delivery: you pay the full value)"))
    lines.append("Max loss at the stop: %d x %s = %s, plus charges %s = %s."
                 % (s["qty"], _price(s["risk_per_share"]), inr(s["qty"] * s["risk_per_share"]),
                    inr(s["charges_at_stop"]), inr(s["max_loss"])))
    b = s["charges_breakdown"]
    lines.append("Charges (what the government and the broker take) if the stop fills: %s = brokerage %s, STT %s, "
                 "exchange %s, SEBI %s, stamp duty %s, GST %s%s."
                 % (inr(b["total"]), inr(b["brokerage"]), inr(b["stt"]), inr(b["exchange_txn"]), inr(b["sebi"]),
                    inr(b["stamp_duty"]), inr(b["gst"]),
                    (", DP charge %s" % inr(b["dp_charge"])) if b["dp_charge"] else ""))
    gross = s["qty"] * s["reward_per_share"]
    lines.append("If the target fills: %s before charges, charges %s (%.0f%% of it), %s after."
                 % (inr(gross), inr(s["charges_at_target"]), 100.0 * s["charges_at_target"] / gross if gross else 0,
                    inr(gross - s["charges_at_target"])))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % _price(s["stop"]))
    lines.append("")
    lines.append("Proposed orders (broker-neutral, shown as Kite place_order fields; nothing has been placed):")
    e = order["entry"]
    lines.append("  BUY %d %s %s, LIMIT %s, %s, DAY" % (e["quantity"], e["tradingsymbol"], e["product"],
                                                        _price(e["price"]), e["exchange"]))
    if order["stop"]:
        lines.append("  then SELL %d %s MIS, SL-M trigger %s (placed after the entry fills)"
                     % (order["stop"]["quantity"], order["stop"]["tradingsymbol"], _price(order["stop"]["trigger_price"])))
        lines.append("  target %s (a LIMIT sell, or an alert: see nse-broker)" % _price(order["target"]["price"]))
    else:
        g = order["gtt_oco"]
        lines.append("  then GTT OCO (placed after the entry fills): stop %s / target %s, sell %d"
                     % (_price(g["trigger_values"][0]), _price(g["trigger_values"][1]), g["orders"][0]["quantity"]))
    if s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets closed (%s auto-squares-off MIS at %s, with a fee; "
                     "nse-alerts rings, nse-broker flatten)." % (s["flat_by"], s["broker_name"], s["squareoff"]))
        if s["squareoff_note"]:
            lines.append("Note: " + s["squareoff_note"])
    lines.append("")
    lines.append("Next: nse-risk checks it, then nse-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- allocation mode (v1)

def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {str(b).upper() for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["ticker"])
        seen.add(r["ticker"])
        if r["ticker"] in blocked:
            dropped.append({"ticker": r["ticker"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"ticker": r["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"ticker": r["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact (unrounded) rupees per stock with the cap enforced.

    Proportional to weight; any stock over the cap is pinned at the cap and
    the rest of the money is shared over the others, repeated until no stock
    is over. Returns ({ticker: rupees}, set of capped tickers)."""
    alloc, capped = {}, set()
    active = [r for r in rows]
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["ticker"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["ticker"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["ticker"]] = cap
            capped.add(r["ticker"])
            money_left -= cap
        active = [r for r in active if r["ticker"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["ticker"]] = 0.0
            break
    return alloc, capped


def round_to_unit(exact, capped, order, cap, unit):
    """Round to paise (unit 0.01) and keep the total.

    The total is the exact total rounded DOWN to the unit, so it never spends
    money you do not have. Each slice is rounded to the nearest unit, then the
    difference goes to the biggest stock that is not capped, never pushing
    anyone over the cap."""
    def units(x):
        return int(math.floor(x / unit + EPS))

    cap_u = units(cap)
    total_u = units(sum(exact.values()))
    out = {}
    for t, v in exact.items():
        out[t] = cap_u if t in capped else int(math.floor(v / unit + 0.5 + EPS))
    diff = total_u - sum(out.values())
    takers = [t for t in order if t not in capped]
    guard = 0
    while diff != 0 and takers and guard < 100000:
        moved = False
        for t in takers:
            if diff == 0:
                break
            if diff > 0 and out[t] + 1 <= cap_u:
                out[t] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[t] > 0:
                out[t] -= 1
                diff += 1
                moved = True
        if not moved:
            break
        guard += 1
    return {t: round(u * unit, 2) for t, u in out.items()}


def whole_shares(rupees, prices, order, cap, cash):
    """Whole shares: floor each slice, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {}
    for t in order:
        shares[t] = int(math.floor(rupees[t] / prices[t] + EPS)) if rupees[t] > 0 else 0
    spent = sum(shares[t] * prices[t] for t in order)
    left = cash - spent
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = rupees[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, budget=None, prices=None, top=None):
    """Allocation mode. Pure function: weights + rules -> the full sizing result (a dict)."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in nse/config.json")
    buf_pct = float(c.get("cash_buffer_pct", 10))
    cap_pct = float(c.get("max_position_pct", 25))
    min_order = float(c.get("min_order_inr", 500) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    prices = prices or {}

    keep, dropped = normalise(rows, c.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100.0, 2)
    unit = 0.01

    missing = [r["ticker"] for r in keep if not prices.get(r["ticker"])]
    if missing:
        raise SizeError("whole shares need a price for: %s (add them to nse/prices.json, "
                        "e.g. with nse-alerts: alerts.py quote %s --write)" % (", ".join(missing), " ".join(missing)))

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["ticker"] for r in sorted(live, key=lambda r: -exact[r["ticker"]])]
        rupees = round_to_unit(exact, capped, order, cap, unit)
        shares = whole_shares(rupees, prices, order, cap, sum(rupees.values()))
        rupees = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if rupees[r["ticker"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["ticker"]))
        if shares[worst["ticker"]] == 0 and prices.get(worst["ticker"], 0) > 0:
            reason = ("one share costs %s, more than its slice; it cannot be bought in whole shares"
                      % inr(prices[worst["ticker"]]))
        else:
            reason = "its slice (%s) is under your %s minimum order" % (inr(rupees[worst["ticker"]]), inr(min_order, 0))
        dropped.append({"ticker": worst["ticker"], "reason": reason})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")

    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["ticker"]
        positions.append({"ticker": t, "name": r["name"], "weight": round(r["weight"], 2),
                          "source_weight": r["source_weight"], "inr": rupees[t], "capped": t in capped,
                          "price": prices[t], "shares": shares[t]})
    invested = round(sum(p["inr"] for p in positions), 2)
    charges = 0.0
    for p in positions:
        if p["shares"] > 0:
            charges += trade_charges(p["shares"] * tick_up(p["price"]), 0, "CNC", "NSE", c)["total"]
    return {
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "investable": investable, "max_position_pct": cap_pct, "cap": cap,
        "min_order_inr": min_order, "positions": positions, "invested": invested,
        "idle": round(investable - invested, 2), "buy_charges": round(charges, 2), "dropped": dropped,
    }


def orders_from(result, source="", cfg=None, now_ist=None):
    """Allocation mode: one CNC buy per position, in the order contract (no stop, no GTT)."""
    cfg = dict(cfg or {})
    out = []
    day = now_ist.strftime("%Y%m%d") if now_ist else ""
    for p in result["positions"]:
        if p["shares"] <= 0:
            continue
        price = tick_up(p["price"])
        why = "%.2f%% of the allocation" % p["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_num(result["max_position_pct"]), inr(result["budget"], 0))
        ch = trade_charges(p["shares"] * price, 0, "CNC", "NSE", cfg)
        o = {
            "id": "%s-%s-ALLOC" % (p["ticker"], day),
            "symbol": p["ticker"], "exchange": "NSE", "yahoo": p["ticker"] + ".NS", "series": "EQ",
            "product": "CNC", "side": "BUY", "qty": int(p["shares"]), "order_type": "LIMIT", "price": price,
            "stop_trigger": None, "target_price": None, "broker": broker_profile(cfg)["key"],
            "entry": {"variety": "regular", "exchange": "NSE", "tradingsymbol": p["ticker"],
                      "transaction_type": "BUY", "quantity": int(p["shares"]), "product": "CNC",
                      "order_type": "LIMIT", "price": price, "validity": "DAY"},
            "stop": None, "target": None, "gtt_oco": None,
            "plan": {"entry": price, "stop": None, "target": None, "r_per_share": None, "setup": "allocation"},
            "max_loss": None, "charges_at_stop": None, "charges_at_target": None, "reward_risk": None,
            "margin_needed": round(p["shares"] * price, 2), "flat_by": None, "why": why,
            "buy_charges": ch["total"],
        }
        o["fingerprint"] = fingerprint(o)
        out.append(o)
    return out


def report(result, orders, source=""):
    """Allocation table."""
    r = result
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (inr(r["budget"], 0), _num(r["cash_buffer_pct"]), inr(r["cash_buffer"], 0),
                    inr(r["investable"], 0), _num(r["max_position_pct"]), inr(r["cap"], 0)))
    lines.append("")
    lines.append("%-12s %-22s %8s %14s %11s %8s  note" % ("symbol", "name", "weight", "rupees", "price", "shares"))
    for p in r["positions"]:
        row = "%-12s %-22s %7.2f%% %14s %11s %8d" % (p["ticker"], (p["name"] or "")[:22], p["weight"],
                                                      inr(p["inr"]), inr(p["price"]), p["shares"])
        if p["capped"]:
            row += "  capped at %s%%" % _num(r["max_position_pct"])
        lines.append(row)
    lines.append("%-12s %-22s %8s %14s" % ("", "invested", "", inr(r["invested"])))
    lines.append("%-12s %-22s %8s %14s" % ("", "cash buffer", "", inr(r["cash_buffer"])))
    lines.append("%-12s %-22s %8s %14s" % ("", "left idle", "", inr(r["idle"])))
    lines.append("%-12s %-22s %8s %14s" % ("", "buy-side charges", "", inr(r["buy_charges"])))
    if r["idle"] >= 1:
        lines.append("")
        lines.append("Left idle %s: whole shares do not divide evenly, or every stock is at its cap "
                     "(add stocks or raise max_position_pct)." % inr(r["idle"]))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["ticker"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (Kite place_order fields, nothing has been placed):")
    for o in orders:
        e = o["entry"]
        lines.append("  BUY %d %s CNC, LIMIT %s, DAY" % (e["quantity"], e["tradingsymbol"], _price(e["price"])))
    lines.append("")
    lines.append("These are delivery buys with no stop. nse-risk warns about that (\"require_stop_cnc\": false) "
                 "and applies your size, position and cash limits to every buy.")
    lines.append("Next: nse-risk checks every order, then nse-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "nse-budget",
        "created_at": iso(now_utc or utc_now()),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "note": ("Proposals only. Nothing is placed until nse-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    now_ist = to_ist(now)
    today = now_ist.date().isoformat()
    note_cal = calendar_note(now_ist.date())
    plan = read_json(a.plan, "plan (run nse-exit first, or use --weights for an allocation)")
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
        else:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 1196.10")
    sym = str((plan or {}).get("symbol") or "").upper()
    yahoo = (plan or {}).get("yahoo")
    notes, price = [], None
    if note_cal:
        notes.append(note_cal)
    fo, fo_note = load_fo(a.state_dir, now_ist.date(), a.offline, a.fo_lots)
    if fo_note:
        notes.append(fo_note)
    try:
        cas = is_cas_stock(fo, parse_symbol(sym)["tradingsymbol"]) if sym else True
    except IndiaError:
        cas = True
    ppath = a.prices or ("nse/prices.json" if os.path.exists("nse/prices.json") else None)
    if ppath and limit is None and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now, yahoo)
        if p is not None and fresh:
            price = p
            if tick_up(p) > tick_up(_f(plan.get("entry")) or 0):
                notes.append("%s last %s (as of %s IST) is above the plan's entry %s, so the risk "
                             "is bigger: sized at %s." % (sym, _price(p), to_ist(t).strftime("%H:%M"),
                                                          _price(plan.get("entry")), _price(tick_up(p))))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_ist(t).strftime("%Y-%m-%d %H:%M") + " IST")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today, a.product, cas)
    order = build_order(s, now_ist, price)
    if order and a.write:
        src = "nse/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed order set to %s" % a.write)
    return 0 if order else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    prices = load_prices(a.prices)
    if not prices and os.path.exists("nse/prices.json") and not a.prices:
        prices = load_prices("nse/prices.json")
    prices = {plain_symbol(k): v for k, v in prices.items()}
    result = size(rows, cfg, a.budget, prices, a.top)
    now = parse_now(a.now)
    orders = orders_from(result, source, cfg, to_ist(now))
    if a.write:
        write_orders(a.write, orders, cfg, source, now)
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Size a trade to your budget and max loss (nse-budget).")
    ap.add_argument("--plan", default="nse/plan.json", help="the trade plan (default nse/plan.json)")
    ap.add_argument("--config", default="nse/config.json", help="rules (default nse/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config (rupees)")
    ap.add_argument("--product", choices=["MIS", "CNC"], type=lambda s: s.upper(),
                    help="MIS intraday or CNC delivery (default: the plan's product)")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="limit price: exactly the plan's entry, or PRICE (default: the plan's entry, or a "
                         "higher fresh price)")
    ap.add_argument("--prices", help="prices JSON (default nse/prices.json if it exists)")
    ap.add_argument("--portfolio", default="nse/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this India time (IST), e.g. \"2026-10-26 09:45\"")
    ap.add_argument("--write", nargs="?", const="nse/orders.json", metavar="PATH",
                    help="write the proposed order (default nse/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    ap.add_argument("--offline", action="store_true", help="no network: the F&O stock list comes from --fo-lots "
                                                            "or the cache in --state-dir")
    ap.add_argument("--fo-lots", help="a local fo_mktlots.csv (default: cached/downloaded into --state-dir)")
    ap.add_argument("--state-dir", default="nse/state", help="where fo_mktlots-YYYY-MM-DD.csv is cached")
    g = ap.add_argument_group("allocation mode (split a budget across a list, CNC)")
    g.add_argument("--weights", help="inline weights: RELIANCE=30,TCS=25")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"ticker\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    a = ap.parse_args(argv)
    try:
        load_calendar()
        cfg = load_config(a.config)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        if a.top:
            raise SizeError("--top works with --weights or --targets")
        return run_plan(a, cfg)
    except IndiaError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
