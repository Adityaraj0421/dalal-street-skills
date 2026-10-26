import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("nse-risk", "check")
ticket = load("nse-broker", "ticket")
size = load("nse-budget", "size")

CFG_PATH = os.path.join(ROOT, "nse-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "nse-risk", "orders-reliance.json")
SEC_PATH = os.path.join(FIX, "nse-risk", "sec_list.csv")
NOW = "2026-10-26 09:45"
TODAY = "2026-10-26"
QUOTE_TIME = datetime.datetime(2026, 10, 26, 4, 25, 13)       # 09:55:13 IST

with open(SEC_PATH, encoding="utf-8") as _fh:
    SECS = check.parse_sec_list(_fh.read())
FO_PATH = os.path.join(FIX, "_shared", "fo_mktlots.csv")
with open(FO_PATH, encoding="utf-8") as _fh:
    FO = check.parse_fo_symbols(_fh.read())          # RELIANCE and TCS are F&O (closing-auction) stocks


def chk(*a, **k):
    k.setdefault("fo", FO)
    return check.check(*a, **k)


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH, encoding="utf-8") as fh:
        c = json.load(fh)
    c.update(over)
    return c


def rel(**over):
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def mk(sym, qty, entry, stop, target, product="MIS", series="EQ", flat_by="15:05", exchange="NSE"):
    """A contract order for any symbol (the legs nse-budget writes)."""
    e = {"variety": "regular", "exchange": exchange, "tradingsymbol": sym, "transaction_type": "BUY",
         "quantity": qty, "product": product, "order_type": "LIMIT", "price": entry, "validity": "DAY"}
    o = {"id": "%s-X" % sym, "symbol": sym, "exchange": exchange, "yahoo": sym + ".NS", "series": series,
         "product": product, "side": "BUY", "qty": qty, "order_type": "LIMIT", "price": entry,
         "stop_trigger": stop, "target_price": target, "entry": e,
         "plan": {"entry": entry, "stop": stop, "target": target}, "target": {"price": target}}
    if product == "MIS":
        o["stop"] = {"variety": "regular", "exchange": exchange, "tradingsymbol": sym, "transaction_type": "SELL",
                     "quantity": qty, "product": "MIS", "order_type": "SL-M", "trigger_price": stop,
                     "validity": "DAY"}
        o["gtt_oco"] = None
        o["flat_by"] = flat_by
    else:
        o["stop"] = None
        sell = {"exchange": exchange, "tradingsymbol": sym, "transaction_type": "SELL", "quantity": qty,
                "order_type": "LIMIT", "product": "CNC"}
        o["gtt_oco"] = {"trigger_type": "two-leg", "tradingsymbol": sym, "exchange": exchange,
                        "trigger_values": [stop, target], "last_price": entry,
                        "orders": [dict(sell, price=stop), dict(sell, price=target)]}
        o["flat_by"] = None
    return o


def sell(sym, qty, product="MIS", typ="MARKET"):
    e = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "SELL",
         "quantity": qty, "product": product, "order_type": typ, "validity": "DAY"}
    return {"id": sym + "-FLAT", "symbol": sym, "exchange": "NSE", "product": product, "entry": e, "stop": None,
            "target": None, "gtt_oco": None, "plan": {}, "flat_by": "15:10" if product == "MIS" else None}


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


def placed(sym, side="buy", day=TODAY, max_loss=993.82, kind=None, status="placed", product="MIS"):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "product": product,
         "at": day + "T09:50:00+05:30"}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if kind:
        r["kind"] = kind
    return r


def run_one(order, now=NOW, secs=SECS, prev=None, **over):
    return chk([order], cfg(**over), now=at(now), secs=secs, prev_close=prev)


class TheReliance(unittest.TestCase):
    def test_the_fixture_passes_every_check(self):
        res = chk([rel()], cfg(), now=at(NOW), secs=SECS)
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual((r["kind"], r["entry_ref"], r["stop"], r["target"]), ("entry", 1196.10, 1188.50, 1211.30))
        self.assertEqual((r["risk_per_share"], r["max_loss"]), (7.60, 993.82))
        self.assertEqual((r["charges_at_stop"], r["charges_at_target"]), (97.02, 97.78))
        self.assertEqual((r["value_inr"], r["margin_inr"]), (141139.8, 28227.96))
        self.assertEqual(r["reward_risk"], 1.71)
        self.assertEqual(res["cash_after"], round(200000 - 28227.96, 2))
        self.assertEqual(res["date"], TODAY)
        self.assertIs(res["paper"], True)

    def test_one_more_share_breaks_the_max_loss_because_of_charges(self):
        o = rel()
        o["entry"] = dict(o["entry"], quantity=119)
        o["stop"] = dict(o["stop"], quantity=119)
        res = chk([o], cfg(), now=at(NOW), secs=SECS)
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("119 shares x 7.60 risk a share", reasons(res))
        self.assertIn("charges", reasons(res))
        self.assertIn("over your ₹1,000 max loss per trade", reasons(res))
        # gross risk alone would fit: 119 x 7.60 = 904.40
        self.assertLess(119 * 7.6, 1000)

    def test_fingerprint_is_shared_with_budget_and_broker(self):
        o = rel()
        self.assertEqual(check.fingerprint(o), ticket.fingerprint(o))
        self.assertEqual(check.fingerprint(o), size.fingerprint(o))
        self.assertEqual(check.fingerprint(dict(o, why="x")), check.fingerprint(o))
        e = json.loads(json.dumps(o))
        e["stop"]["trigger_price"] = 1188.0
        self.assertNotEqual(check.fingerprint(e), check.fingerprint(o))
        self.assertEqual(len(check.fingerprint(o)), 16)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", none, "--state", none, "--offline", "--seclist", SEC_PATH, "--fo-lots", FO_PATH,
                                   "--now", NOW, "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("PASS  #1 buy RELIANCE 118 shares MIS, entry", text)
            self.assertIn("entry 1196.10 (the limit price), stop 1188.50, target 1211.30: risk 7.60 a share", text)
            self.assertIn("max loss ₹993.82 = ₹896.80 + charges ₹97.02 (limit ₹1,000)", text)
            self.assertIn("reward:risk 1.71 after charges (min 1)", text)
            self.assertIn("1 passed, 0 blocked at 2026-10-26 09:45 IST", text)
            self.assertIn("not financial advice", text)
            with open(out, encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual((rec["date"], rec["results"][0]["kind"]), (TODAY, "entry"))
            self.assertEqual(rec["results"][0]["fingerprint"], check.fingerprint(rel()))

    def test_errors_are_one_line(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", "/nonexistent.json", "--no-write"]), 1)
        self.assertIn("no orders file", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", ORDERS_PATH, "--now", "noon", "--no-write", "--offline"]), 1)
        self.assertIn("--now wants", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", ORDERS_PATH, "--today", "monday", "--no-write", "--offline"]), 1)
        self.assertIn("--today wants", err.getvalue())

    def test_without_sec_list_it_says_so(self):
        res = chk([rel()], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("NOT checked", " ".join(res["warnings"]))


class Instrument(unittest.TestCase):
    def test_be_and_bz_series_refuse_mis(self):
        o = mk("ABAN", 100, 30.0, 29.0, 32.0, series="BE")
        res = run_one(o)
        self.assertIn("series: ABAN trades in series BE (trade-for-trade)", reasons(res))
        o = mk("AGSTRA", 100, 30.0, 29.0, 32.0, series="BZ")
        self.assertIn("series BZ", reasons(run_one(o)))
        # the sec_list series wins over what the order says
        o = mk("ABAN", 100, 30.0, 29.0, 32.0, series="EQ")
        self.assertIn("series BE", reasons(run_one(o)))

    def test_be_is_fine_for_delivery(self):
        o = mk("ABAN", 100, 30.0, 29.5, 30.5, product="CNC", series="BE")
        res = run_one(o, prev={"ABAN": 30.0}, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))

    def test_gsm_blocks_mis_and_warns_cnc(self):
        o = mk("AQYLON", 100, 50.0, 49.0, 53.0)
        res = run_one(o, prev={"AQYLON": 50.0})
        self.assertIn("gsm/asm: AQYLON is under surveillance (GSM STAGE - 0): MIS is blocked", reasons(res))
        o = mk("AQYLON", 100, 50.0, 49.0, 52.0, product="CNC")
        res = run_one(o, prev={"AQYLON": 50.0})
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("under surveillance (GSM STAGE - 0)", warnings(res))

    def test_circuit_band(self):
        prev = {"A2ZINFRA": 100.0}                                   # band 5%: 95.00 to 105.00
        ok = mk("A2ZINFRA", 100, 101.0, 99.0, 104.0)
        res = run_one(ok, prev=prev)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("circuit band 95.00 to 105.00", " ".join(res["results"][0]["info"]))
        res = run_one(mk("A2ZINFRA", 100, 106.0, 104.0, 109.0), prev=prev)
        self.assertIn("circuit: the entry 106.00 is above the upper circuit 105.00", reasons(res))
        res = run_one(mk("A2ZINFRA", 100, 97.0, 94.0, 99.0), prev=prev)
        self.assertIn("circuit: the stop 94.00 is below the lower circuit 95.00", reasons(res))
        res = run_one(mk("A2ZINFRA", 100, 102.0, 100.0, 107.0), prev=prev)
        self.assertIn("circuit: the target 107.00 is above the upper circuit 105.00", reasons(res))
        self.assertNotIn("entry", reasons(res))

    def test_band_limits_use_the_tick_grid(self):
        self.assertEqual(check.band_limits(123.45, 5), (117.30, 129.60))
        self.assertEqual(check.band_limits(100.0, 2), (98.0, 102.0))

    def test_circuit_needs_a_previous_close_and_skips_no_band(self):
        res = run_one(mk("A2ZINFRA", 100, 101.0, 99.0, 104.0))
        self.assertIn("no previous close for A2ZINFRA", warnings(res))
        self.assertEqual(verdicts(res), ["PASS"])
        res = run_one(rel(), prev={"RELIANCE": 1190.0})
        self.assertIn("no fixed price band", warnings(res))

    def test_previous_close_can_ride_on_the_order(self):
        o = dict(mk("A2ZINFRA", 100, 106.0, 104.0, 109.0), prev_close=100.0)
        self.assertIn("circuit:", reasons(run_one(o)))

    def test_prev_close_from_a_yahoo_chart(self):
        base = int(datetime.datetime(2026, 10, 22, 4, 0).replace(tzinfo=datetime.timezone.utc).timestamp())
        day = 86400
        doc = {"chart": {"result": [{"meta": {"gmtoffset": 19800}, "timestamp": [base, base + day, base + 4 * day],
                                     "indicators": {"quote": [{"close": [1180.0, 1190.0, 1198.0]}]}}]}}
        self.assertEqual(check.prev_close_from_chart(doc, datetime.date(2026, 10, 26)), 1190.0)
        self.assertEqual(check.prev_close_from_chart(doc, datetime.date(2026, 10, 23)), 1180.0)
        self.assertIsNone(check.prev_close_from_chart({}, datetime.date(2026, 10, 26)))

    def test_sec_list_parsing_handles_spaces_and_no_band(self):
        s = check.parse_sec_list(' Symbol , Series ,Security Name, Band , Remarks \nXYZ ,EQ, X Y Z LTD ,No Band,"-"\n'
                                 'ABC,EQ,A B C,10,"ASM LT - Stage I"\n')
        self.assertEqual(s["XYZ"], {"series": "EQ", "band": None, "remarks": "", "name": "X Y Z LTD"})
        self.assertEqual((s["ABC"]["band"], s["ABC"]["remarks"]), (10.0, "ASM LT - Stage I"))
        res = run_one(mk("ABC", 10, 50.0, 49.0, 52.0), secs=s)
        self.assertIn("gsm/asm", reasons(res))

    def test_unknown_symbol_and_bse_warn(self):
        res = run_one(mk("ZZTOP", 10, 100.0, 99.0, 102.0))
        self.assertIn("not in sec_list.csv", warnings(res))
        res = run_one(mk("RELIANCE", 10, 1196.1, 1188.5, 1211.3, exchange="BSE"))
        self.assertIn("BSE order", warnings(res))

    def test_fno_and_odd_symbols(self):
        self.assertIn("asset:", reasons(run_one(mk("NIFTY26OCT24000CE", 1, 100.0, 90.0, 120.0))))
        o = mk("RELIANCE", 10, 1196.1, 1188.5, 1211.3, exchange="NFO")
        self.assertIn("asset:", reasons(run_one(o)))
        self.assertIn("derivatives", warnings(run_one(o, allow_fno=True)))
        self.assertIn("asset:", reasons(run_one(mk("BTC/USD", 1, 100.0, 90.0, 120.0))))


class WindowAndFlat(unittest.TestCase):
    def test_mis_entry_window_is_ist(self):
        # A2ZINFRA is not an F&O stock, so its cutoff is the configured 14:45
        def a2z(**kw):
            return mk("A2ZINFRA", 100, 101.0, 99.0, 104.0, **kw)
        prev = {"A2ZINFRA": 100.0}
        cases = [("2026-10-26 09:20", "no new entries before 09:30 IST (it is 09:20)"),
                 ("2026-10-26 14:46", "no new entries after 14:45 IST (it is 14:46)"),
                 ("2026-10-26 09:00", "the market is closed (Mon 2026-10-26 09:00 IST; the session is 09:15 to 15:30)"),
                 ("2026-10-26 15:35", "the market is closed"),
                 ("2026-10-24 10:15", "the market is closed today (weekend"),
                 ("2026-11-10 10:15", "the market is closed today (NSE holiday: Diwali-Balipratipada")]
        for now, want in cases:
            res = run_one(a2z(), now=now, prev=prev)
            self.assertIn("window: " + want, reasons(res), now)
        for now in ("2026-10-26 09:30", "2026-10-26 14:45", "2026-10-26T04:10:00Z", "2026-10-26 10:15"):
            self.assertEqual(verdicts(run_one(a2z(), now=now, prev=prev, min_reward_risk=0)), ["PASS"], now)

    def test_no_mis_entry_after_continuous_close_minus_the_buffer(self):
        # RELIANCE is an F&O (closing-auction) stock: continuous trading ends 15:15 until 2026-12-06,
        # so with a 45-minute buffer the last entry is 14:30, not 14:45
        res = run_one(rel(), now="2026-10-26 14:31")
        self.assertIn("no new entries after 14:30 IST (it is 14:31) (continuous trading in this stock ends at 15:15, "
                      "and your entry buffer is 45 minutes)", reasons(res))
        self.assertEqual(verdicts(run_one(rel(), now="2026-10-26 14:30")), ["PASS"])
        self.assertEqual(verdicts(run_one(rel(), now="2026-10-26 14:31", entry_buffer_min=30)), ["PASS"])
        # from 2026-12-07 every stock trades to 15:30 again: 14:45
        res = run_one(rel(flat_by="15:00"), now="2026-12-07 14:46", squareoff={"cas": "15:12", "other": "15:25"})
        self.assertIn("no new entries after 14:45 IST", reasons(res))
        self.assertEqual(verdicts(run_one(rel(flat_by="15:00"), now="2026-12-07 14:40",
                                          squareoff={"cas": "15:12", "other": "15:25"})), ["PASS"])
        # a stock that is not in the F&O list keeps 14:45
        self.assertEqual(verdicts(run_one(mk("A2ZINFRA", 100, 101.0, 99.0, 104.0), now="2026-10-26 14:40",
                                          prev={"A2ZINFRA": 100.0}, min_reward_risk=0)), ["PASS"])

    def test_the_window_uses_india_time_not_the_machine(self):
        self.assertEqual(check.to_ist(at("2026-10-26 09:45")), datetime.datetime(2026, 10, 26, 9, 45))
        self.assertEqual(at("2026-10-26 09:45"), datetime.datetime(2026, 10, 26, 4, 15))
        self.assertEqual(at("2026-12-01 09:45"), datetime.datetime(2026, 12, 1, 4, 15))

    def test_window_can_be_switched_off(self):
        res = run_one(rel(), now="2026-10-24 10:15", no_entries_before=None, no_entries_after=None)
        self.assertEqual(verdicts(res), ["PASS"])
        with self.assertRaises(check.RiskError):
            run_one(rel(), no_entries_before="9.30am")

    def test_cnc_is_not_blocked_by_the_day_window(self):
        o = mk("RELIANCE", 10, 1196.1, 1188.5, 1211.3, product="CNC")
        for now in ("2026-10-26 09:20", "2026-10-26 15:00"):
            self.assertEqual(verdicts(run_one(o, now=now, min_reward_risk=0)), ["PASS"], now)
        res = run_one(o, now="2026-10-24 11:00", min_reward_risk=0)                    # Saturday: it would queue
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("would queue as an after-market order", warnings(res))

    def test_flat_by_follows_the_broker_square_off_for_the_stock_and_day(self):
        # RELIANCE (F&O): Zerodha squares off CAS stocks at 15:12 -> flat_by at or before 15:07
        self.assertEqual(check.default_flat_by(datetime.date(2026, 10, 26), True, {})[0], datetime.time(15, 7))
        self.assertEqual(verdicts(run_one(rel(flat_by="15:07"))), ["PASS"])
        res = run_one(rel(flat_by="15:08"))
        self.assertIn("flat_by: 15:08 is too late; it must be at or before 15:07 (Zerodha squares off MIS at 15:12",
                      reasons(res))
        # a non-F&O stock: square-off 15:25 -> 15:20, but never later than the config's flat_by 15:10
        o = mk("A2ZINFRA", 100, 101.0, 99.0, 104.0, flat_by="15:10")
        res = run_one(o, prev={"A2ZINFRA": 100.0}, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        o = mk("A2ZINFRA", 100, 101.0, 99.0, 104.0, flat_by="15:11")
        self.assertIn("must be at or before 15:10", reasons(run_one(o, prev={"A2ZINFRA": 100.0}, min_reward_risk=0)))
        res = run_one(o, prev={"A2ZINFRA": 100.0}, min_reward_risk=0, flat_by="15:19")
        self.assertEqual(verdicts(res), ["PASS"])                   # the user's own flat_by moved the limit to 15:20
        self.assertIn("must carry a flat_by", reasons(run_one(rel(flat_by=None))))
        self.assertIn("flat_by:", reasons(run_one(rel(flat_by="soon"))))
        self.assertIn("Zerodha MIS square-off 15:12 (F&O closing-auction stock): flat_by must be at or before 15:07",
                      " ".join(run_one(rel())["results"][0]["info"]))

    def test_the_dec_7_switch_back_to_one_close(self):
        d1, d2 = datetime.date(2026, 12, 4), datetime.date(2026, 12, 8)
        self.assertEqual(check.continuous_close(d1, True), datetime.time(15, 15))
        self.assertEqual(check.continuous_close(datetime.date(2026, 12, 7), True), datetime.time(15, 30))
        self.assertEqual(check.default_flat_by(d1, True, {})[0], datetime.time(15, 7))
        fb, so, note = check.default_flat_by(d2, True, {})
        self.assertEqual((fb, so), (datetime.time(15, 10), datetime.time(15, 25)))
        self.assertIn("before SEBI's 2026-12-07 auction change", note)          # the printed square-off note
        res = run_one(rel(flat_by="15:10"), now="2026-12-08 10:15")
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("before SEBI's 2026-12-07 auction change", " ".join(res["warnings"]))
        # the user's own square-off times silence the note
        res = run_one(rel(flat_by="15:10"), now="2026-12-08 10:15", squareoff={"cas": "15:12", "other": "15:25"})
        self.assertNotIn("auction change", " ".join(res["warnings"]))

    def test_the_square_off_note_is_printed_for_unverified_brokers(self):
        res = run_one(rel(flat_by="15:00"), broker="upstox")
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))        # upstox CAS 15:10 -> 15:05
        res = run_one(rel(flat_by="15:00"), broker="icicidirect")
        self.assertIn("square-off time is not verified", " ".join(res["warnings"]))
        self.assertIn("must be at or before 14:55", reasons(run_one(rel(flat_by="15:00"), broker="motilal")))

    def test_unknown_broker_is_refused(self):
        with self.assertRaises(check.IndiaError) as e:
            run_one(rel(), broker="mystery")
        self.assertIn("unknown broker 'mystery'", str(e.exception))
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "c.json")
            with open(c, "w") as fh:
                json.dump({"broker": "mystery", "budget": 200000}, fh)
            with redirect_stderr(err):
                self.assertEqual(check.main(["--orders", ORDERS_PATH, "--config", c, "--no-write", "--offline",
                                             "--seclist", SEC_PATH, "--fo-lots", FO_PATH, "--now", NOW]), 1)
        self.assertIn("unknown broker", err.getvalue())

    def test_an_entry_at_or_after_its_own_flat_by_is_blocked(self):
        res = run_one(rel(flat_by="09:40"), now="2026-10-26 09:45")
        self.assertIn("at or past this order's flat_by 09:40", reasons(res))

    def test_special_session_muhurat(self):
        # 2026-11-08 is built in: 14:20-15:30. Entries from 14:35 to close-15 (15:15; a CAS stock 15:00)
        self.assertIn("2026-11-08", check.SPECIAL_SESSIONS)
        o = rel(flat_by="15:05")
        res = run_one(o, now="2026-11-08 14:30")
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("no new entries before 14:35", reasons(res))
        res = run_one(o, now="2026-11-08 14:40")
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("special session", warnings(res))
        self.assertIn("no new entries after 15:00", reasons(run_one(o, now="2026-11-08 15:01")))
        self.assertIn("the market is closed (Sun 2026-11-08 14:00 IST; the session is 14:20 to 15:30)",
                      reasons(run_one(o, now="2026-11-08 14:00")))

    def test_calendar_note_outside_2026(self):
        res = chk([rel()], cfg(), now=at("2027-02-01 10:00"), secs=SECS)
        self.assertIn("holiday list covers 2026 only", " ".join(res["warnings"]))


class RulesAndCaps(unittest.TestCase):
    def test_a_stop_is_required_for_mis_not_for_cnc(self):
        o = rel(stop=None)
        o["stop_trigger"] = None
        o["plan"] = {"entry": 1196.10}
        res = run_one(o)
        self.assertIn("stop: no stop. Every MIS entry needs one", reasons(res))
        self.assertEqual(verdicts(run_one(o, require_stop=False, min_reward_risk=0)), ["PASS"])
        cnc = mk("RELIANCE", 10, 1196.1, 1188.5, 1211.3, product="CNC")
        cnc["gtt_oco"] = None
        cnc["stop_trigger"] = None
        cnc["plan"] = {"entry": 1196.10}
        res = run_one(cnc, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop", warnings(res))
        self.assertIn("stop: no stop. Every CNC entry needs one", reasons(run_one(cnc, require_stop_cnc=True)))

    def test_a_plan_stop_counts_but_warns(self):
        o = rel(stop=None)
        o["stop_trigger"] = None
        res = run_one(o)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("in the plan only", warnings(res))

    def test_stop_and_target_on_the_right_side(self):
        o = rel()
        o["stop"] = dict(o["stop"], trigger_price=1197.0)
        self.assertIn("must be below the entry 1196.10", reasons(run_one(o)))
        o = rel(target={"price": 1196.0})
        self.assertIn("target: the target 1196.00 must be above the entry", reasons(run_one(o)))

    def test_reward_risk_is_net_of_charges(self):
        o = rel(target={"price": 1200.0})
        self.assertIn("reward:risk:", reasons(run_one(o)))
        self.assertIn("after charges", reasons(run_one(o)))
        self.assertEqual(verdicts(run_one(o, min_reward_risk=0.2)), ["PASS"])

    def test_loss_includes_the_cnc_delivery_charges(self):
        o = mk("RELIANCE", 120, 1196.1, 1188.5, 1211.3, product="CNC")
        res = run_one(o)
        r = res["results"][0]
        ch = check.trade_charges(120 * 1196.1, 120 * 1188.5, "CNC", "NSE", None)["total"]
        self.assertEqual(r["charges_at_stop"], ch)
        self.assertEqual(r["max_loss"], round(120 * 7.6 + ch, 2))
        self.assertGreater(ch, 150)                       # STT both sides + DP charge

    def test_a_fresh_higher_price_is_the_entry_for_market_orders(self):
        o = rel()
        o["entry"] = dict(o["entry"], order_type="MARKET", price=None)
        o["order_type"], o["price"] = "MARKET", None
        res = chk([o], cfg(), prices={"RELIANCE": 1198.55}, now=at("2026-10-26 09:56"), secs=SECS,
                          price_times={"RELIANCE": QUOTE_TIME})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1198.55, "price", 10.05))
        self.assertIn("loss:", reasons(res))
        res = chk([o], cfg(), prices={"RELIANCE": 1198.55}, now=at("2026-10-26 11:30"), secs=SECS,
                          price_times={"RELIANCE": QUOTE_TIME})
        self.assertEqual(res["results"][0]["entry_ref"], 1196.10)
        self.assertIn("older than 15 minutes", warnings(res))

    def test_daily_loss_and_trades_per_day_count_mis_only(self):
        two = {"orders": [placed("AAA"), placed("BBB")]}
        res = chk([rel()], cfg(), state=two, now=at(NOW), secs=SECS)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 2 of 3 MIS trades, ₹1,987.64 of ₹3,000 daily loss", res["results"][0]["info"][-1])
        three = {"orders": [placed("AAA"), placed("BBB"), placed("CCC")]}
        why = reasons(chk([rel()], cfg(), state=three, now=at(NOW), secs=SECS))
        self.assertIn("trades: 3 MIS entries placed today", why)
        self.assertIn("daily loss: ₹2,981.46 already at risk today plus ₹993.82 for this one = ₹3,975.28, over your ₹3,000", why)
        yesterday = {"orders": [placed(s, day="2026-10-23") for s in "ABC"]}
        self.assertEqual(verdicts(chk([rel()], cfg(), state=yesterday, now=at(NOW), secs=SECS)), ["PASS"])
        cnc_days = {"orders": [placed(s, product="CNC") for s in "ABCD"]}
        self.assertEqual(verdicts(chk([rel()], cfg(), state=cnc_days, now=at(NOW), secs=SECS)), ["PASS"])

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [mk(s, 100, 50.0, 45.0, 60.0) for s in ("AAA", "BBB", "CCC", "DDD")]
        res = chk(orders, cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        self.assertIn("trades: 0 MIS entries placed today plus 3 passed earlier", reasons(res, 3))

    def test_exits_and_declines_do_not_count(self):
        st = {"orders": [placed("AAA"), placed("AAA", side="sell", kind="exit", max_loss=None),
                         placed("BBB", status="declined"), placed("CCC", status="failed")]}
        res = chk([rel()], cfg(), state=st, now=at(NOW), secs=SECS)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["mis_entries_placed"], 1)

    def test_size_position_cash_and_margin(self):
        big = mk("RELIANCE", 400, 1196.1, 1195.0, 1200.0)
        res = run_one(big)
        self.assertIn("over your ₹4,00,000 max order", reasons(res))
        res = run_one(mk("RELIANCE", 1, 1196.1, 1188.5, 1211.3), min_order_inr=5000)
        self.assertIn("under your ₹5,000 minimum order", reasons(res))
        # MIS margin is value / 5; CNC needs the full value
        cnc = mk("RELIANCE", 100, 1196.1, 1188.5, 1211.3, product="CNC")
        res = run_one(cnc, max_position_pct=100, cash_buffer_pct=0, max_loss_per_trade_inr=5000)
        self.assertEqual(res["results"][0]["margin_inr"], 119610.0)
        mis = mk("RELIANCE", 300, 1196.1, 1195.0, 1210.0)
        res = run_one(mis, max_loss_per_trade_inr=5000, min_reward_risk=0, max_order_inr=0)
        self.assertEqual(res["results"][0]["margin_inr"], 71766.0)
        res = chk([cnc], cfg(max_position_pct=100, max_loss_per_trade_inr=5000), portfolio={"cash": 50000, "positions": []},
                          now=at(NOW), secs=SECS)
        self.assertIn("cash: needs ₹1,19,610.00 cash but only ₹50,000.00", reasons(res))
        res = chk([rel()], cfg(), portfolio={"cash": 30000, "positions": []}, now=at(NOW), secs=SECS)
        self.assertIn("buffer", reasons(res))
        res = chk([rel()], cfg(max_position_pct=10), now=at(NOW), secs=SECS)
        self.assertIn("position: RELIANCE would tie up ₹28,227.96", reasons(res))

    def test_blocked_list(self):
        res = run_one(rel(), blocked=["reliance"])
        self.assertIn("blocked: RELIANCE is on your blocked list", reasons(res))

    def test_duplicates_and_daily_order_limit(self):
        res = chk([rel(), rel()], cfg(), now=at(NOW), secs=SECS)
        self.assertEqual(verdicts(res), ["PASS", "BLOCK"])
        self.assertIn("duplicate: a second buy RELIANCE (MIS) in this batch", reasons(res, 1))
        st = {"orders": [placed("T%d" % i, max_loss=1) for i in range(10)]}
        self.assertIn("daily limit", reasons(chk([rel()], cfg(max_trades_per_day=0), state=st, now=at(NOW), secs=SECS)))
        st = {"orders": [placed("RELIANCE")]}
        self.assertIn("already placed today", reasons(chk([rel()], cfg(), state=st, now=at(NOW), secs=SECS)))

    def test_live_account_warns(self):
        res = chk([rel()], cfg(paper=False), now=at(NOW), secs=SECS)
        self.assertIn("LIVE", res["warnings"][0])

    def test_no_fo_list_means_the_cautious_flat_by(self):
        res = check.check([rel(flat_by="15:10")], cfg(), now=at(NOW), secs=SECS)
        self.assertIn("F&O stock list was not loaded", " ".join(res["warnings"]))
        self.assertIn("flat_by: 15:10 is too late", reasons(res))


class FormatChecks(unittest.TestCase):
    def one(self, order, **over):
        return run_one(order, **over)

    def test_format(self):
        o = rel()
        o["entry"] = dict(o["entry"], quantity=118.5)
        self.assertIn("whole number of shares", reasons(self.one(o)))
        o = rel()
        o["entry"] = dict(o["entry"], order_type="LIMIT", price=None)
        self.assertIn("a LIMIT order needs a price", reasons(self.one(o)))
        o = rel()
        o["entry"] = dict(o["entry"], product="NRML")
        self.assertIn("product must be MIS", reasons(self.one(o)))
        o = rel()
        o["entry"] = dict(o["entry"], transaction_type="HOLD")
        self.assertIn("transaction_type must be BUY or SELL", reasons(self.one(o)))
        o = rel()
        o["entry"] = dict(o["entry"], order_type="ICEBERG")
        self.assertIn("order_type must be one of", reasons(self.one(o)))
        o = rel()
        o["entry"] = dict(o["entry"], price=1196.12)
        self.assertIn("not a multiple of 0.05", warnings(self.one(o)))
        self.assertIn("no entry leg", reasons(self.one({"symbol": "X"})))

    def test_stop_leg_must_match_the_entry(self):
        o = rel()
        o["stop"] = dict(o["stop"], quantity=100)
        self.assertIn("stop leg quantity", reasons(self.one(o)))
        o = rel()
        o["stop"] = dict(o["stop"], transaction_type="BUY")
        self.assertIn("other side of the entry", reasons(self.one(o)))
        o = rel()
        o["stop"] = dict(o["stop"], order_type="LIMIT")
        self.assertIn("SL-M (or SL)", reasons(self.one(o)))

    def test_gtt_oco_shape(self):
        o = mk("RELIANCE", 10, 1196.1, 1188.5, 1211.3, product="CNC")
        self.assertEqual(verdicts(self.one(o, min_reward_risk=0.5)), ["PASS"], reasons(self.one(o, min_reward_risk=0.5)))
        bad = json.loads(json.dumps(o))
        bad["gtt_oco"]["trigger_values"] = [1211.3, 1188.5]
        self.assertIn("GTT OCO needs", reasons(self.one(bad, min_reward_risk=0.5)))
        bad = json.loads(json.dumps(o))
        bad["gtt_oco"]["orders"] = bad["gtt_oco"]["orders"][:1]
        self.assertIn("exactly two orders", reasons(self.one(bad, min_reward_risk=0.5)))
        mis = rel()
        mis["gtt_oco"] = o["gtt_oco"]
        self.assertIn("cannot protect an MIS position", reasons(self.one(mis)))

    def test_a_broker_neutral_order_without_legs_is_checked(self):
        o = {k: v for k, v in rel().items() if k not in ("entry", "stop", "gtt_oco", "target")}
        res = self.one(o)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertEqual(res["results"][0]["max_loss"], 993.82)
        self.assertEqual(res["results"][0]["target"], 1211.3)


class Exits(unittest.TestCase):
    PF = {"cash": 171772.04, "positions": [{"ticker": "RELIANCE", "product": "MIS", "shares": 118, "margin": 28227.96,
                                             "lots": [{"date": TODAY, "shares": 118, "cost": 141139.8}]}]}

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        full = {"orders": [placed("AAA"), placed("BBB"), placed("CCC")] +
                          [placed("T%d" % i, max_loss=1) for i in range(6)] +
                          [placed("RELIANCE", side="sell", kind="exit", max_loss=None)]}
        c = cfg(blocked=["RELIANCE"], max_order_inr=100, max_daily_loss_inr=50)
        res = chk([sell("RELIANCE", 118)], c, portfolio=self.PF, state=full, now=at("2026-10-26 15:25"),
                          prices={"RELIANCE": 1200.0}, secs=SECS)
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        why = warnings(res)
        for word in ("blocked:", "daily limit:", "size:"):
            self.assertIn("exit, not blocked: " + word, why)
        res = chk([sell("RELIANCE", 118)], cfg(), portfolio=self.PF, now=at("2026-10-24 12:00"),
                          prices={"RELIANCE": 1200.0})
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_partial_exit_and_the_rest(self):
        half = sell("RELIANCE", 59)
        res = chk([half, half, half], cfg(), portfolio=self.PF, prices={"RELIANCE": 1200.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("short:", reasons(res, 2))

    def test_cnc_cannot_be_sold_short_ever(self):
        pf = {"cash": 100000, "positions": [{"ticker": "TCS", "product": "CNC", "shares": 10,
                                             "lots": [{"date": "2026-01-02", "shares": 10, "cost": 30000}]}]}
        res = chk([sell("TCS", 11, "CNC")], cfg(allow_short=True), portfolio=pf, prices={"TCS": 3100.0}, now=at(NOW))
        self.assertIn("CNC cannot be sold short", reasons(res))
        res = chk([sell("TCS", 10, "CNC")], cfg(), portfolio=pf, prices={"TCS": 3100.0}, now=at(NOW))
        self.assertEqual((verdicts(res), res["results"][0]["kind"]), (["PASS"], "exit"))
        res = chk([sell("INFY", 1, "CNC")], cfg(), portfolio=pf, prices={"INFY": 1480.0}, now=at(NOW))
        self.assertIn("short:", reasons(res))
        # CNC shares are not MIS shares
        res = chk([sell("TCS", 5, "MIS")], cfg(), portfolio=pf, prices={"TCS": 3100.0}, now=at(NOW))
        self.assertIn("short selling is off", reasons(res))

    def test_mis_short_only_with_allow_short(self):
        res = chk([sell("RELIANCE", 10)], cfg(), now=at(NOW))
        self.assertIn("short selling is off", reasons(res))
        self.assertIn("record the fill first", reasons(res))

    def test_format_still_applies_to_a_sell(self):
        o = sell("RELIANCE", 118, typ="LIMIT")
        res = chk([o], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("a LIMIT order needs a price", reasons(res))

    def test_buy_then_sell_in_one_batch(self):
        res = chk([rel(), sell("RELIANCE", 118)], cfg(), now=at(NOW), secs=SECS, prices={"RELIANCE": 1200.0})
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")


class Cli(unittest.TestCase):
    def test_prices_file_freshness_and_keys(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-10-26T04:26:00Z", "prices": {"RELIANCE.NS": 1198.55},
                           "detail": {"RELIANCE.NS": {"time": "2026-10-26T04:25:13Z", "prev_close": 1190.0}}}, fh)
            prices, times, prevs = check.load_prices(json.load(open(pp, encoding="utf-8")))
            self.assertEqual((prices, prevs), ({"RELIANCE": 1198.55}, {"RELIANCE": 1190.0}))
            self.assertEqual(times["RELIANCE"], QUOTE_TIME)

    def test_blocked_orders_exit_one_and_are_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "orders.json")
            with open(bad, "w") as fh:
                json.dump({"orders": [rel(flat_by="15:25")]}, fh)
            out = os.path.join(d, "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", bad, "--config", CFG_PATH, "--portfolio", none, "--prices", none,
                                   "--state", none, "--offline", "--seclist", SEC_PATH, "--fo-lots", FO_PATH, "--now", NOW, "--out", out])
            self.assertEqual(code, 1)
            self.assertIn("BLOCK #1 buy RELIANCE", buf.getvalue())
            self.assertIn("Blocked orders do not go to the broker", buf.getvalue())
            with open(out, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["block"], 1)


class BrokerProfiles(unittest.TestCase):
    def test_profile_comes_from_the_core(self):
        self.assertEqual(check.broker_profile({"broker": "Zerodha"})["key"], "zerodha")
        self.assertEqual(check.broker_profile({"broker": "Angel One"})["key"], "angelone")
        self.assertEqual(check.broker_key({}), "zerodha")
        self.assertEqual(len(check.BROKERS), 10)

    def test_charges_are_per_the_configured_broker(self):
        zer = run_one(rel())["results"][0]["charges_at_stop"]
        dhan = run_one(rel(), broker="dhan")["results"][0]["charges_at_stop"]
        upstox = run_one(rel(), broker="upstox")["results"][0]["charges_at_stop"]
        self.assertEqual(zer, 97.02)
        self.assertEqual(dhan, zer)                       # 0.03% capped at Rs 20, like Zerodha
        self.assertEqual(upstox, zer)                     # Rs 20 cap on both sides

    def test_profile_charges_are_overridden_by_the_users(self):
        res = run_one(rel(), charges={"gst_pct": 0})
        self.assertLess(res["results"][0]["max_loss"], 993.82)


if __name__ == "__main__":
    unittest.main()
