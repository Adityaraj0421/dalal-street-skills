import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import types
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

alerts = load("nse-alerts", "alerts")

QDIR = os.path.join(FIX, "nse-alerts")     # Yahoo chart JSON fetched 2026-10-26 10:37 IST, plus a sec_list.csv excerpt
PLAN = os.path.join(QDIR, "plan-reliance.json")
DT = datetime.datetime
UA = "Mozilla/5.0 (compatible; india-trader-skill/1.0)"
MUHURAT = {"open": datetime.time(18, 0), "close": datetime.time(19, 0), "name": "Muhurat trading"}


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules(bands=None, **changes):
    with open(PLAN) as fh:
        plan = json.load(fh)
    plan.update(changes)
    return alerts.rules_from_plan(plan, None, bands)


def sec_bands():
    return alerts.parse_sec_list(read("sec_list.csv"))


def read(name):
    with open(os.path.join(QDIR, name), encoding="utf-8") as fh:
        return fh.read()


class Special(object):
    """Registers the 2026-11-08 Muhurat session for a test, and removes it again."""

    def __enter__(self):
        self.saved = alerts.SPECIAL_SESSIONS.get("2026-11-08")
        alerts.SPECIAL_SESSIONS["2026-11-08"] = dict(MUHURAT)

    def __exit__(self, *exc):
        alerts.SPECIAL_SESSIONS["2026-11-08"] = self.saved


class Calendar(unittest.TestCase):
    def test_holidays(self):
        for d, word in (("2026-10-20", "Dussehra"), ("2026-11-10", "Diwali"), ("2026-12-25", "Christmas"),
                        ("2026-01-26", "Republic Day"), ("2026-10-02", "Gandhi")):
            ok, why = alerts.trading_day(datetime.date.fromisoformat(d))
            self.assertFalse(ok, d)
            self.assertIn(word, why)
        for d in alerts.NSE_HOLIDAYS:
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5, d)
        self.assertEqual(alerts.CALENDAR_YEARS, (2026,))

    def test_ist_has_no_daylight_saving(self):
        self.assertEqual(ist("2026-10-26T09:15"), DT(2026, 10, 26, 3, 45))
        self.assertEqual(ist("2026-07-01 09:15"), DT(2026, 7, 1, 3, 45))
        self.assertEqual(alerts.to_ist(DT(2026, 12, 1, 3, 45)), DT(2026, 12, 1, 9, 15))

    def test_parse_now(self):
        self.assertEqual(ist("2026-10-26 10:15"), DT(2026, 10, 26, 4, 45))
        self.assertEqual(ist("2026-10-26 10:15 IST"), DT(2026, 10, 26, 4, 45))
        self.assertEqual(ist("2026-10-26T20:00:01Z"), DT(2026, 10, 26, 20, 0, 1))
        self.assertEqual(ist("2026-10-26T10:15+05:30"), DT(2026, 10, 26, 4, 45))
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_market_status(self):
        st = alerts.market_status(ist("2026-10-26T10:00"))
        self.assertTrue(st["open"])
        self.assertEqual((st["opens"], st["closes"], st["reason"]), ("09:15", "15:30", "regular session"))
        early = alerts.market_status(ist("2026-10-26T09:14"))
        self.assertFalse(early["open"])
        self.assertEqual(early["next_open"], "2026-10-26 09:15")
        self.assertIn("pre-open", alerts.market_status(ist("2026-10-26T09:05"))["reason"])
        self.assertEqual(alerts.market_status(ist("2026-10-26T08:00"))["reason"], "before the open")
        self.assertTrue(alerts.market_status(ist("2026-10-26T15:29"))["open"])
        after = alerts.market_status(ist("2026-10-26T15:30"))
        self.assertFalse(after["open"])
        self.assertEqual((after["reason"], after["next_open"]), ("after the close", "2026-10-27 09:15"))
        dus = alerts.market_status(ist("2026-10-20T11:00"))
        self.assertFalse(dus["open"])
        self.assertIn("Dussehra", dus["reason"])
        self.assertEqual(dus["next_open"], "2026-10-21 09:15")
        sat = alerts.market_status(ist("2026-10-24T10:00"))
        self.assertEqual((sat["reason"], sat["next_open"]), ("weekend", "2026-10-26 09:15"))
        self.assertEqual(alerts.market_status(ist("2026-10-23T16:00"))["next_open"], "2026-10-26 09:15")
        self.assertEqual(alerts.market_status(ist("2026-11-09T15:31"))["next_open"], "2026-11-11 09:15")  # 11-10 is Diwali-Balipratipada
        self.assertFalse(alerts.market_status(ist("2027-03-01T10:00"))["calendar_known"])
        self.assertTrue(alerts.market_status(ist("2026-10-26T10:00"))["calendar_known"])

    def test_muhurat_session(self):
        # the real 2026 timings are built in: 14:20 to 15:30, F&O stocks to 15:15
        st = alerts.market_status(ist("2026-11-08T14:30"))
        self.assertTrue(st["open"])
        self.assertEqual((st["opens"], st["closes"], st["fo_close"], st["special"]),
                         ("14:20", "15:30", "15:15", "Diwali Muhurat trading"))
        self.assertIn("14:20 to 15:30 IST", alerts.market_status(ist("2026-10-26T10:00"))["muhurat_note"])
        self.assertNotIn("muhurat_note", alerts.market_status(ist("2026-12-01T10:00")))
        pre = alerts.market_status(ist("2026-11-08T14:00"))
        self.assertFalse(pre["open"])
        self.assertEqual(pre["next_open"], "2026-11-08 14:20")
        self.assertIn("special session starts at 14:20", pre["reason"])
        self.assertTrue(alerts.market_status(ist("2026-11-08T15:29"))["open"])
        self.assertFalse(alerts.market_status(ist("2026-11-08T15:30"))["open"])
        # timings NSE has not announced: a note says so
        saved = alerts.SPECIAL_SESSIONS.pop("2026-11-08")
        try:
            st = alerts.market_status(ist("2026-11-08T18:30"))
            self.assertFalse(st["open"])
            self.assertIn("Diwali Muhurat trading is expected on 2026-11-08", st["muhurat_note"])
            self.assertIn("nse/holidays.json", st["muhurat_note"])
        finally:
            alerts.SPECIAL_SESSIONS["2026-11-08"] = saved
        with Special():
            self.assertIn("Muhurat trading on 2026-11-08: 18:00 to 19:00", alerts.market_status(ist("2026-10-26T10:00"))["muhurat_note"])
            st = alerts.market_status(ist("2026-11-08T18:30"))
            self.assertTrue(st["open"])
            self.assertEqual((st["opens"], st["closes"], st["reason"], st["special"]),
                             ("18:00", "19:00", "special session", "Muhurat trading"))
            pre = alerts.market_status(ist("2026-11-08T17:00"))
            self.assertFalse(pre["open"])
            self.assertEqual(pre["next_open"], "2026-11-08 18:00")
            self.assertEqual(alerts.market_status(ist("2026-11-08T19:00"))["next_open"], "2026-11-09 09:15")
            # the normal morning of that Sunday is still closed
            self.assertFalse(alerts.market_status(ist("2026-11-08T10:00"))["open"])
            self.assertEqual(alerts.market_status(ist("2026-10-30T16:00"))["next_open"], "2026-11-02 09:15")


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        q = alerts.parse_yahoo(read("yahoo-RELIANCE.json"), "RELIANCE")
        self.assertEqual((q["price"], q["prev_close"], q["open"]), (1195.6, 1190.0, 1186.0999755859375))
        self.assertEqual((q["time"], q["currency"], q["yahoo"]), ("2026-10-26T05:07:48Z", "INR", "RELIANCE.NS"))
        q = alerts.parse_yahoo(read("yahoo-JPPOWER.json"), "JPPOWER")
        self.assertEqual((q["price"], q["prev_close"]), (13.18, 12.67))
        q = alerts.parse_yahoo(read("yahoo-M&M.json"), "M&M")
        self.assertEqual(q["yahoo"], "M&M.NS")
        with self.assertRaises(alerts.AlertError) as cm:
            alerts.parse_yahoo(read("yahoo-ZZZZNOPE.json"), "ZZZZNOPE")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(alerts.AlertError):
            alerts.parse_yahoo("<html>", "X")

    def test_keys_and_urls(self):
        self.assertEqual(alerts.UA, UA)
        for text, key in (("reliance", "RELIANCE"), ("TCS.NS", "TCS"), ("NSE:INFY", "INFY"),
                          ("RELIANCE.BO", "RELIANCE.BO"), ("m&m", "M&M"), ("nifty", "NIFTY"), ("^NSEBANK", "^NSEBANK")):
            self.assertEqual(alerts.skey(text), key, text)
        self.assertEqual(alerts.skey("tcs", "BSE"), "TCS.BO")
        with self.assertRaises(alerts.AlertError):
            alerts.skey("not a ticker!")
        self.assertEqual(alerts.chart_path("RELIANCE.NS", "1d", "5m"), "/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", alerts.chart_path("M&M.NS", "1d", "5m"))
        self.assertIn("/chart/%5ENSEI?", alerts.chart_path("^NSEI", "1d", "5m"))

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes(["RELIANCE", "tcs.ns", "M&M", "ZZZZNOPE", "NOFILE"], QDIR)
        self.assertEqual(sorted(q), ["M&M", "RELIANCE", "TCS"])
        self.assertEqual(q["TCS"]["price"], 2529.5)
        self.assertEqual(len(errors), 2)
        self.assertIn("No data found", errors[0])
        self.assertIn("no offline file", errors[1])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-JPPOWER.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-JPPOWER.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["JPPOWER"], d)
            self.assertEqual((q["JPPOWER"]["price"], errors), (13.18, []))
        finally:
            shutil.rmtree(d)


class FakeNet(object):
    """Stands in for the network: answers each request with the next status code."""

    def __init__(self, codes):
        self.codes, self.calls, self.sleeps = list(codes), [], []

    def open_url(self, url, timeout=20, accept="application/json"):
        self.calls.append(url)
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(url, code, "x", {}, fp)
        if "sec_list" in url:
            return read("sec_list.csv")
        return read("yahoo-M&M.json" if "M%26M" in url else "yahoo-JPPOWER.json" if "JPPOWER" in url
                    else "yahoo-RELIANCE.json")


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._open_url, alerts._sleep, alerts.time)

    def tearDown(self):
        alerts._open_url, alerts._sleep, alerts.time = self.saved

    def net(self, codes):
        f = FakeNet(codes)
        alerts._open_url = f.open_url
        alerts._sleep = f.sleeps.append
        alerts.time = types.SimpleNamespace(sleep=f.sleeps.append, time=self.saved[2].time)
        return f

    def hosts(self, f):
        return [u.split("//")[1].split(".")[0] for u in f.calls]

    def test_query2_first_and_the_url(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual((q["RELIANCE"]["price"], errors), (1195.6, []))
        self.assertEqual(f.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m"])

    def test_an_ampersand_ticker_is_escaped(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["M&M"])
        self.assertEqual(errors, [])
        self.assertIn("/chart/M%26M.NS?", f.calls[0])

    def test_bse_and_index_go_to_the_right_symbol(self):
        f = self.net([200, 200])
        alerts.get_quotes(["RELIANCE.BO", "NIFTY"])
        self.assertIn("/chart/RELIANCE.BO?", f.calls[0])
        self.assertIn("/chart/%5ENSEI?", f.calls[1])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes(["RELIANCE"])[0]["RELIANCE"]["price"], 1195.6)
        self.assertEqual(self.hosts(f), ["query2", "query2"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query1_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        f = self.net([503, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query1"])
        f = self.net([500, 502])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual(q, {})
        self.assertIn("Yahoo did not answer", errors[0])
        self.assertIn("HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes(["NOPE"])
        self.assertIn("No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])
        f = self.net([403])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])

    def test_a_pause_between_tickers(self):
        f = self.net([200, 200])
        alerts.get_quotes(["RELIANCE", "JPPOWER"])
        self.assertEqual(f.sleeps, [alerts.PAUSE])

    def test_stooq_is_gone(self):
        self.assertFalse(hasattr(alerts, "stooq_url"))
        with self.assertRaises(SystemExit):
            run(["quote", "RELIANCE", "--source", "stooq"])


class Bands(unittest.TestCase):
    def test_sec_list_fixture(self):
        b = alerts.parse_sec_list(read("sec_list.csv"))
        self.assertIsNone(b["RELIANCE"]["band"])                     # "No Band": dynamic bands
        self.assertIsNone(b["M&M"]["band"])
        self.assertEqual(b["JPPOWER"], {"band": 20.0, "series": "EQ", "remarks": ""})
        self.assertEqual(b["A2ZINFRA"]["band"], 5.0)
        self.assertEqual(b["ABDL"]["band"], 10.0)
        self.assertEqual((b["BLUECHIP"]["series"], b["BLUECHIP"]["band"], b["BLUECHIP"]["remarks"]),
                         ("BE", 2.0, "GSM STAGE - II"))
        self.assertEqual(b["AQYLON"]["remarks"], "GSM STAGE - 0")
        self.assertEqual(b["ELECTCAST"]["series"], "EQ")
        rev = "Symbol,Series,Security Name,Band,Remarks\nX,W1,X LTD,20,\"-\"\nX,EQ,X LTD,10,\"-\"\n"
        self.assertEqual(alerts.parse_sec_list(rev)["X"]["band"], 10.0)      # EQ wins over an earlier row
        spaced = "Symbol , Series , Security Name , Band , Remarks\n Y , EQ , Y LTD , 5 , -\n"
        self.assertEqual(alerts.parse_sec_list(spaced)["Y"]["band"], 5.0)
        with self.assertRaises(alerts.AlertError):
            alerts.parse_sec_list("<html>blocked</html>")
        with self.assertRaises(alerts.AlertError):
            alerts.parse_sec_list("Symbol,Series,Security Name,Band,Remarks\n")

    def test_limits_sit_on_the_tick_inside_the_band(self):
        self.assertEqual(alerts.circuit_limits(12.67, 20), (10.15, 15.20))
        self.assertEqual(alerts.circuit_limits(12.52, 5), (11.90, 13.10))
        self.assertEqual(alerts.circuit_limits(100.0, 10), (90.0, 110.0))
        self.assertEqual(alerts.circuit_limits(1190.0, 2), (1166.2, 1213.8))

    def test_fetched_once_a_day_and_cached(self):
        saved = alerts._open_url
        calls = []
        alerts._open_url = lambda url, timeout=20, accept="application/json": calls.append(url) or read("sec_list.csv")
        d = tempfile.mkdtemp()
        cache = os.path.join(d, "state", "sec_list.json")
        try:
            b, warn = alerts.get_bands(cache, ist("2026-10-26 10:00"))
            self.assertEqual((b["JPPOWER"]["band"], warn), (20.0, None))
            self.assertEqual(calls, ["https://archives.nseindia.com/content/equities/sec_list.csv"])
            b, warn = alerts.get_bands(cache, ist("2026-10-26 14:00"))          # same India date: no second fetch
            self.assertEqual((len(calls), b["A2ZINFRA"]["band"]), (1, 5.0))
            alerts.get_bands(cache, ist("2026-10-27 09:30"))                    # next day: fetched again
            self.assertEqual(len(calls), 2)
            with open(cache) as fh:
                self.assertEqual(json.load(fh)["fetched"], "2026-10-27")
            # the cache is by India date: 20:00 UTC on 2026-10-26 is already 2026-10-27 in India
            alerts.get_bands(cache, DT(2026, 10, 26, 20, 0))
            self.assertEqual(len(calls), 2)

            def down(url, timeout=20, accept=None):
                raise urllib.error.URLError("offline")
            alerts._open_url = down
            b, warn = alerts.get_bands(cache, ist("2026-10-28 10:00"))          # stale copy beats nothing, and says so
            self.assertEqual(b["JPPOWER"]["band"], 20.0)
            self.assertIn("using the copy from 2026-10-27", warn)
            os.remove(cache)
            with self.assertRaises(alerts.AlertError) as cm:
                alerts.get_bands(cache, ist("2026-10-28 10:00"))
            self.assertIn("price band list", str(cm.exception))
        finally:
            alerts._open_url = saved
            shutil.rmtree(d)

    def test_offline_reads_the_folder_and_writes_no_cache(self):
        b, warn = alerts.get_bands("/nonexistent/x.json", ist("2026-10-26 10:00"), QDIR)
        self.assertEqual((b["JPPOWER"]["band"], warn), (20.0, None))


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["RELIANCE", "TCS", "JPPOWER", "A2ZINFRA"], QDIR)
        self.bands = alerts.parse_sec_list(read("sec_list.csv"))
        self.open = alerts.market_status(ist("2026-10-26T11:00"))
        self.now = ist("2026-10-26T11:00")

    def at(self, text, rules, state=None, quotes=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now),
                               state or {}, now, self.bands)

    def test_a_target_rings_once(self):
        rules = [{"id": "rel-1190", "symbol": "RELIANCE", "type": "above", "price": 1190}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertIn("RELIANCE is ₹1,195.60: at or above your alert level ₹1,190", rings[0]["message"])
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])

    def test_prices_use_indian_digit_grouping(self):
        q = json.loads(json.dumps(self.q))
        q["TCS"]["price"] = 1234567.5
        rules = [{"id": "t", "symbol": "TCS", "type": "above", "price": 1000000}]
        rings, _ = alerts.evaluate(rules, q, self.open, {}, self.now)
        self.assertIn("₹12,34,567.50", rings[0]["message"])
        self.assertIn("₹10,00,000", rings[0]["message"])

    def test_stop_and_rearm(self):
        rules = [{"id": "jp-stop", "symbol": "JPPOWER", "type": "stop", "price": 13.5, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("your stop ₹13.50", rings[0]["message"])
        self.q["JPPOWER"]["price"] = 14.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("jp-stop", st["rung"])
        self.q["JPPOWER"]["price"] = 13.4
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(len(rings), 1)

    def test_a_closed_market_says_last(self):
        rules = [{"id": "x", "symbol": "RELIANCE", "type": "above", "price": 1000}]
        rings, _ = self.at("2026-10-26T16:00", rules)
        self.assertTrue(rings[0]["message"].startswith("RELIANCE last ₹1,195.60"))

    def test_pct_move_from_previous_close(self):
        big = [{"id": "jp-5", "symbol": "JPPOWER", "type": "pct_move", "pct": 5}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        small = [{"id": "jp-3", "symbol": "JPPOWER", "type": "pct_move", "pct": 3, "direction": "up"}]
        rings, st = alerts.evaluate(small, self.q, self.open, {}, self.now)
        self.assertIn("JPPOWER moved +4.03% to ₹13.18 (from ₹12.67 previous close)", rings[0]["message"])
        self.assertEqual(alerts.evaluate(small, self.q, self.open, st, self.now)[0], [])      # once a day
        rings, _ = self.at("2026-10-27 11:00", small, st)
        self.assertEqual(len(rings), 1)                         # a new India date rings again
        down = [{"id": "jp-down", "symbol": "JPPOWER", "type": "pct_move", "pct": 3, "direction": "down"}]
        self.assertEqual(alerts.evaluate(down, self.q, self.open, {}, self.now)[0], [])
        small_move = [{"id": "rel-1", "symbol": "RELIANCE", "type": "pct_move", "pct": 0.4}]
        rings, _ = alerts.evaluate(small_move, self.q, self.open, {}, self.now)
        self.assertIn("RELIANCE moved +0.47% to ₹1,195.60 (from ₹1,190.00 previous close)", rings[0]["message"])
        # a ref beats the previous close, and no previous close means no guess
        ref = [{"id": "r", "symbol": "RELIANCE", "type": "pct_move", "pct": 1, "ref": 1180}]
        rings, _ = alerts.evaluate(ref, self.q, self.open, {}, self.now)
        self.assertIn("(from ₹1,180.00)", rings[0]["message"])
        nq = json.loads(json.dumps(self.q))
        nq["JPPOWER"]["prev_close"] = None
        self.assertEqual(alerts.evaluate(small, nq, self.open, {}, self.now)[0], [])

    def test_opening_bell_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        closed = alerts.market_status(ist("2026-10-26T08:00"))
        self.assertEqual(alerts.evaluate(rules, {}, closed, {}, ist("2026-10-26T08:00"))[0], [])
        rings, st = alerts.evaluate(rules, {}, self.open, {}, self.now)
        self.assertIn("Opening bell: the NSE is open (2026-10-26 11:00 IST, regular session 09:15 to 15:30)", rings[0]["message"])
        self.assertEqual(alerts.evaluate(rules, {}, self.open, st, ist("2026-10-26T12:00"))[0], [])
        nxt = alerts.market_status(ist("2026-10-27T09:16"))
        self.assertEqual(len(alerts.evaluate(rules, {}, nxt, st, ist("2026-10-27T09:16"))[0]), 1)
        holiday = alerts.market_status(ist("2026-10-20T09:16"))
        self.assertEqual(alerts.evaluate(rules, {}, holiday, {}, ist("2026-10-20T09:16"))[0], [])

    def test_the_bell_on_a_muhurat_day(self):
        with Special():
            now = ist("2026-11-08T18:01")
            rings, _ = alerts.evaluate([{"id": "bell", "type": "opening_bell"}], {}, alerts.market_status(now), {}, now)
            self.assertIn("Muhurat trading session 18:00 to 19:00", rings[0]["message"])

    def test_the_plan_gives_a_stop_a_target_and_flat_by(self):
        rules = plan_rules(sec_bands())       # RELIANCE is "No Band": no circuit alert
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("reliance-plan-stop", "stop"), ("reliance-plan-target", "target"),
                          ("reliance-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (1183.65, 1203.55))
        self.assertEqual((rules[2]["at"], rules[2]["label"], rules[2]["before_close"], rules[2]["product"]),
                         ("15:10", "flat by", True, "MIS"))
        self.assertTrue(all(r["date"] == "2026-10-26" for r in rules))
        rings, st = self.at("2026-10-26 10:52", rules)          # the real quote, 1195.60: between stop and target
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["price"] = 1183.50
        rings, st = self.at("2026-10-26 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-stop"])
        self.assertIn("RELIANCE is ₹1,183.50: at or below your stop ₹1,183.65 (plan stop: opening-range midpoint)",
                      rings[0]["message"])
        q["RELIANCE"]["price"] = 1203.55
        self.assertEqual([r["id"] for r in self.at("2026-10-26 12:00", rules, st, q)[0]], ["reliance-plan-target"])

    def test_a_fixed_band_stock_gets_a_circuit_alert_from_the_plan(self):
        rules = plan_rules(sec_bands(), symbol="JPPOWER", yahoo="JPPOWER.NS")
        self.assertEqual([r["type"] for r in rules], ["stop", "target", "circuit", "time"])
        self.assertEqual(rules[2], {"id": "jppower-circuit", "symbol": "JPPOWER", "type": "circuit", "date": "2026-10-26"})
        self.assertEqual(alerts.no_circuit_reason("JPPOWER", sec_bands()), None)
        self.assertIn("no fixed price band", alerts.no_circuit_reason("RELIANCE", sec_bands()))
        self.assertIn("not in NSE's price band list", alerts.no_circuit_reason("INFY", sec_bands()))
        # without a band list there is no circuit alert
        self.assertEqual([r["type"] for r in plan_rules(None, symbol="JPPOWER", yahoo="JPPOWER.NS")], ["stop", "target", "time"])

    def test_a_cnc_plan_has_no_flat_by_and_a_bse_plan_no_circuit(self):
        self.assertEqual([r["type"] for r in plan_rules(sec_bands(), product="CNC", symbol="JPPOWER", yahoo="JPPOWER.NS")],
                         ["stop", "target", "circuit"])
        bse = plan_rules(sec_bands(), exchange="BSE", yahoo="RELIANCE.BO")
        self.assertEqual([r["type"] for r in bse], ["stop", "target", "time"])
        self.assertEqual(bse[0]["symbol"], "RELIANCE.BO")
        self.assertEqual(bse[0]["id"], "reliance.bo-plan-stop")
        # the older plan shape (no product, exchange, yahoo) still works: MIS, NSE, 15:10
        with open(PLAN) as fh:
            old = {k: v for k, v in json.load(fh).items() if k not in ("product", "exchange", "yahoo", "flat_by")}
        rules = alerts.rules_from_plan(old)
        self.assertEqual(rules[-1]["at"], "15:10")
        self.assertEqual(alerts.rules_from_plan(old, "14:55")[-1]["at"], "14:55")

    def test_flat_by_rings_once_per_trading_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-10-26 15:09", rules)[0], [])
        rings, st = self.at("2026-10-26 15:10", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("Flat by: it is 15:10 IST (set for 15:10), the market closes at 15:30.", rings[0]["message"])
        self.assertIn("If RELIANCE is still open, close it: an open MIS position is squared off by the broker from "
                      "about 15:20", rings[0]["message"])
        self.assertEqual(self.at("2026-10-26 15:14", rules, st)[0], [])
        rings, _ = self.at("2026-10-26 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-10-27 15:11", rules, st)[0], [])     # plan rules: their day only

    def test_flat_by_follows_a_muhurat_session(self):
        rule = [{"id": "flat", "type": "time", "at": "15:10", "label": "flat by", "before_close": True}]
        with Special():
            self.assertEqual(self.at("2026-11-08 18:54", rule)[0], [])
            rings, _ = self.at("2026-11-08 18:55", rule)
            self.assertIn("set for 18:55), the market closes at 19:00", rings[0]["message"])
            plain = [{"id": "t", "type": "time", "at": "18:50", "label": "check"}]
            self.assertEqual(len(self.at("2026-11-08 18:50", plain)[0]), 1)
        self.assertEqual(self.at("2026-10-24 15:11", rule)[0], [])         # Saturday: no session
        self.assertEqual(self.at("2026-10-20 15:11", rule)[0], [])         # Dussehra
        rings, st = self.at("2026-10-28 15:10", rule)
        self.assertEqual(len(rings), 1)
        self.assertEqual(st["rung"]["flat"], "2026-10-28T09:40:00Z")      # stored in UTC

    def test_circuit_proximity(self):
        rule = [{"id": "jp-c", "symbol": "JPPOWER", "type": "circuit"}]      # 20 % band, previous close 12.67: 10.15 / 15.20
        self.assertEqual(self.at("2026-10-26 11:00", rule)[0], [])           # +4 % is nowhere near
        q = json.loads(json.dumps(self.q))
        q["JPPOWER"]["price"] = 15.10
        rings, st = self.at("2026-10-26 11:00", rule, None, q)
        self.assertEqual([r["id"] for r in rings], ["jp-c:upper"])
        self.assertIn("JPPOWER is ₹15.10, 0.66 % from its upper circuit ₹15.20 (20 % band on the previous close ₹12.67)",
                      rings[0]["message"])
        self.assertIn("Orders can sit unfilled at a circuit", rings[0]["message"])
        self.assertEqual(self.at("2026-10-26 11:05", rule, st, q)[0], [])                       # once a day
        q["JPPOWER"]["price"] = 15.20
        self.assertEqual(self.at("2026-10-26 11:06", rule, st, q)[0], [])                       # still the same ring today
        rings, _ = self.at("2026-10-26 11:06", rule, None, q)
        self.assertIn("is ₹15.20, at its upper circuit ₹15.20", rings[0]["message"])
        rings, _ = self.at("2026-10-27 11:00", rule, st, q)                                     # a new day rings again
        self.assertEqual(len(rings), 1)
        q["JPPOWER"]["price"] = 10.20
        rings, st = self.at("2026-10-26 11:00", rule, None, q)
        self.assertEqual([r["id"] for r in rings], ["jp-c:lower"])
        self.assertIn("0.49 % from its lower circuit ₹10.15", rings[0]["message"])
        self.assertIn("there may be no buyers, so a stop may not get you out", rings[0]["message"])
        q["JPPOWER"]["price"] = 10.15
        self.assertIn("at its lower circuit ₹10.15", self.at("2026-10-26 11:00", rule, None, q)[0][0]["message"])

    def test_circuit_options(self):
        q = json.loads(json.dumps(self.q))
        q["JPPOWER"]["price"] = 14.50
        wide = [{"id": "w", "symbol": "JPPOWER", "type": "circuit", "within": 5}]
        self.assertEqual([r["id"] for r in self.at("2026-10-26 11:00", wide, None, q)[0]], ["w:upper"])
        lower_only = [{"id": "l", "symbol": "JPPOWER", "type": "circuit", "within": 5, "side": "lower"}]
        self.assertEqual(self.at("2026-10-26 11:00", lower_only, None, q)[0], [])
        # the default distance is min(1 %, a quarter of the band): 5 % band -> 1 %, 2 % band -> 0.5 %
        q["A2ZINFRA"]["price"] = 13.00                       # upper circuit 13.10: 0.77 % away
        rule = [{"id": "a", "symbol": "A2ZINFRA", "type": "circuit"}]
        self.assertEqual([r["id"] for r in self.at("2026-10-26 11:00", rule, None, q)[0]], ["a:upper"])
        q["A2ZINFRA"]["price"] = 12.95                       # 1.16 % away
        self.assertEqual(self.at("2026-10-26 11:00", rule, None, q)[0], [])

    def test_circuit_no_band_and_unknown_symbols(self):
        rule = [{"id": "rel-c", "symbol": "RELIANCE", "type": "circuit"}]
        rings, st = self.at("2026-10-26 11:00", rule)
        self.assertIn("RELIANCE has no fixed price band", rings[0]["message"])
        self.assertIn("dynamic bands", rings[0]["message"])
        self.assertEqual(self.at("2026-10-26 11:30", rule, st)[0], [])                 # said once a day
        unknown = [{"id": "u", "symbol": "INFY", "type": "circuit"}]
        q = dict(self.q, INFY=self.q["TCS"])
        self.assertEqual(self.at("2026-10-26 11:00", unknown, None, q)[0], [])          # INFY is not in the band list
        none = alerts.evaluate(rule, self.q, self.open, {}, self.now)                   # no bands passed at all
        self.assertEqual(none[0], [])

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "nse-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("reliance-")], plan_rules())
        self.assertEqual([r["id"] for r in rules if r["type"] == "circuit"], ["jppower-circuit"])

    def test_bad_rules(self):
        for bad in ({"id": "x", "type": "moon"},
                    {"id": "x", "type": "above", "symbol": "RELIANCE"},
                    {"id": "x", "type": "time", "at": "4pm"},
                    {"id": "x", "type": "time", "at": "25:00"},
                    {"id": "x", "type": "circuit", "symbol": "RELIANCE.BO"},
                    {"id": "x", "type": "circuit", "symbol": "NIFTY"},
                    {"id": "x", "type": "circuit", "symbol": "TCS", "side": "middle"},
                    {"id": "x", "type": "circuit", "symbol": "TCS", "within": -1},
                    {"id": "x", "type": "stop", "symbol": "TCS", "price": 1, "date": "tomorrow"}):
            with self.assertRaises(alerts.AlertError, msg=bad):
                alerts.evaluate([bad], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1183.65})
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1, "target": 2, "flat_by": "soon"})


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", QDIR, "--now", now])

    def test_the_reliance_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none"), "--offline", QDIR])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-flat-by", text)
        self.assertNotIn("reliance-circuit", text)
        self.assertIn("Note: RELIANCE has no fixed price band", text)
        self.assertIn("no circuit alert was set", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)

        code, text, _ = self.check("2026-10-26 10:52")
        self.assertEqual(code, 0)                                      # the opening bell rings first
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-26 10:53")
        self.assertEqual(code, 1)
        self.assertIn("NSE OPEN (2026-10-26 10:53 IST, regular session).", text)
        self.assertIn("Nothing hit. 4 rule(s) watched today, RELIANCE ₹1,195.60.", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"RELIANCE": 1195.6})
        self.assertEqual(doc["detail"]["RELIANCE"]["time"], "2026-10-26T05:07:48Z")
        self.assertEqual(doc["detail"]["RELIANCE"]["fetched"], "2026-10-26T05:23:00Z")
        self.assertEqual((doc["detail"]["RELIANCE"]["yahoo"], doc["detail"]["RELIANCE"]["exchange"]),
                         ("RELIANCE.NS", "NSE"))
        self.assertEqual(doc["detail"]["RELIANCE"]["prev_close"], 1190.0)

        code, text, _ = self.check("2026-10-26 15:10")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by: it is 15:10 IST", text)
        self.assertIn("not an order", text)
        self.assertEqual(self.check("2026-10-26 15:12")[0], 1)
        code, text, _ = self.check("2026-10-27 10:00")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-27 10:05")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                              "--offline", QDIR])
        with open(self.rules) as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "reliance-flat-by", "reliance-plan-stop", "reliance-plan-target"])
        with open(self.state) as fh:
            self.assertNotIn("reliance-flat-by", json.load(fh)["rung"])

    def test_a_cnc_plan_from_the_cli(self):
        cnc = os.path.join(self.d, "plan.json")
        with open(PLAN) as fh:
            plan = json.load(fh)
        plan["product"] = "CNC"
        with open(cnc, "w") as fh:
            json.dump(plan, fh)
        code, text, _ = run(["add", "--from-plan", "--plan", cnc, "--rules", self.rules, "--state", self.state,
                             "--offline", QDIR])
        self.assertEqual(code, 0)
        self.assertNotIn("flat-by", text.split("CNC (delivery)")[0])
        self.assertIn("CNC (delivery) plan: no flat-by alert", text)

    def test_the_evening_and_morning(self):
        for args in (["--opening-bell"], ["--symbol", "JPPOWER", "--above", "14"], ["--symbol", "A2ZINFRA", "--stop", "12"]):
            code, _, _ = run(["add", "--rules", self.rules] + args)
            self.assertEqual(code, 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "JPPOWER", "--above", "14"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        code, text, _ = self.check("2026-10-26T20:00")
        self.assertEqual(code, 1)
        self.assertIn("NSE closed (2026-10-26 20:00 IST, after the close).", text)
        self.assertIn("Nothing hit", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"A2ZINFRA": 12.65, "JPPOWER": 13.18})

        code, text, _ = self.check("2026-10-27T09:16")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-27T09:45")
        self.assertEqual(code, 1)

        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_circuit_rule_from_the_cli(self):
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "jppower", "--circuit", "--within", "0.5", "--offline", QDIR])
        self.assertEqual(code, 0)
        self.assertIn("Added jppower-circuit-0.50", text)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "A2ZINFRA", "--circuit", "--side", "upper", "--offline", QDIR])
        self.assertEqual(code, 0)
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "jppower-circuit-0.50", "symbol": "JPPOWER", "type": "circuit", "within": 0.5})
        self.assertEqual(rules[1]["side"], "upper")
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "RELIANCE.BO", "--circuit"])
        self.assertEqual(code, 1)
        self.assertIn("NSE stocks only", err)
        code, text, _ = self.check("2026-10-26T11:00")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 2 rule(s) watched today, JPPOWER ₹13.18, A2ZINFRA ₹12.65.", text)

    def test_check_names_a_missing_band_list(self):
        run(["add", "--rules", self.rules, "--symbol", "JPPOWER", "--circuit", "--offline", QDIR])
        d = tempfile.mkdtemp()
        try:
            shutil.copy(os.path.join(QDIR, "yahoo-JPPOWER.json"), d)
            code, text, _ = run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                                 "--offline", d, "--now", "2026-10-26T11:00"])
            self.assertEqual(code, 1)
            self.assertIn("error: no offline file", text)
        finally:
            shutil.rmtree(d)

    def test_a_circuit_alert_on_a_no_band_stock_is_refused(self):
        for sym in ("RELIANCE", "tcs", "M&M"):
            code, _, err = run(["add", "--rules", self.rules, "--symbol", sym, "--circuit", "--offline", QDIR])
            self.assertEqual(code, 1, sym)
            self.assertIn("has no fixed price band", err)
            self.assertIn("a circuit alert cannot be set", err)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "INFY", "--circuit", "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("not in NSE's price band list", err)
        self.assertFalse(os.path.exists(self.rules))

    def test_a_plan_written_by_the_real_exit_py(self):
        # nse/plan.json as nse-exit/exit.py plan --write produced it (entry_bar is null, entry_time "10:00 AM")
        plan = os.path.join(QDIR, "plan-from-exit.json")
        code, text, err = run(["add", "--from-plan", "--plan", plan, "--rules", self.rules, "--state", self.state,
                               "--config", os.path.join(self.d, "none"), "--offline", QDIR])
        self.assertEqual(code, 0, err)
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("reliance-plan-stop", "stop"), ("reliance-plan-target", "target"), ("reliance-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"], rules[2]["at"]), (1183.65, 1203.55, "15:10"))
        self.assertEqual(rules[0]["note"], "plan stop: opening-range midpoint")
        self.assertTrue(all(r["date"] == "2026-10-26" for r in rules))

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--at", "15:10", "--label", "flat by",
                             "--before-close", "--symbol", "reliance"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "RELIANCE", "--target", "1203.55",
                             "--date", "2026-10-26"])
        self.assertEqual(code, 0)
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1510", "type": "time", "at": "15:10", "label": "flat by",
                                    "symbol": "RELIANCE", "before_close": True})
        self.assertEqual(rules[1], {"id": "reliance-target-1203.55", "symbol": "RELIANCE", "type": "target",
                                    "price": 1203.55, "date": "2026-10-26"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "4pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "RELIANCE", "tcs.ns", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-10-26T10:38"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE   ₹1,195.60 (+0.47% vs prev close ₹1,190.00)", text)
        self.assertIn("delayed", text)
        run(["quote", "M&M", "--offline", QDIR, "--write", self.prices])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["M&M", "RELIANCE", "TCS"])
        self.assertEqual(doc["detail"]["TCS"]["prev_close"], 2530.0)
        self.assertEqual(doc["detail"]["M&M"]["yahoo"], "M&M.NS")
        self.assertEqual(doc["detail"]["RELIANCE"]["fetched"], "2026-10-26T05:08:00Z")
        self.assertIn("rupees", doc["note"])

    def test_quote_json_and_errors(self):
        code, text, _ = run(["quote", "RELIANCE", "NOPE", "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(list(out["quotes"]), ["RELIANCE"])
        self.assertEqual(len(out["errors"]), 1)
        self.assertEqual(run(["quote", "NOPE", "--offline", QDIR])[0], 1)
        code, _, err = run(["quote"])
        self.assertEqual(code, 1)
        self.assertIn("which tickers", err)

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"ticker": "TCS", "shares": 1}],
                       "holdings": [{"symbol": "jppower", "qty": 100}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["JPPOWER", "RELIANCE", "TCS"])

    def test_clock(self):
        self.assertEqual(run(["clock", "--now", "2026-10-26 10:15"])[0], 0)
        code, text, _ = run(["clock", "--now", "2026-10-26 10:15"])
        self.assertIn("India 2026-10-26 10:15 (Monday): NSE OPEN, regular session.", text)
        self.assertIn("Today's session: 09:15 to 15:30 IST.", text)
        code, text, _ = run(["clock", "--now", "2026-10-20T10:00"])
        self.assertEqual(code, 1)
        self.assertIn("NSE holiday: Dussehra", text)
        self.assertIn("Next open: 2026-10-21 09:15 IST.", text)
        code, text, _ = run(["clock", "--now", "2026-10-26T15:45"])
        self.assertIn("Next open: 2026-10-27 09:15 IST.", text)
        code, text, _ = run(["clock", "--now", "2027-02-01T10:00"])
        self.assertIn("covers 2026 only", text)
        code, text, _ = run(["clock", "--now", "2026-10-26 10:15", "--json"])
        self.assertTrue(json.loads(text)["open"])
        code, _, err = run(["clock", "--now", "later"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("alerts.py: "))

    def test_holidays_json_adds_a_muhurat_session(self):
        # the user adds NSE's announced timings to nse/holidays.json; the clock then knows the session
        cwd = os.getcwd()
        os.makedirs(os.path.join(self.d, "nse"))
        with open(os.path.join(self.d, "nse", "holidays.json"), "w") as fh:
            json.dump({"special_sessions": {"2026-11-08": {"open": "18:00", "close": "19:00",
                                                           "name": "Muhurat trading"}}}, fh)
        saved = alerts._calendar_loaded
        saved_sp = alerts.SPECIAL_SESSIONS["2026-11-08"]
        os.chdir(self.d)
        try:
            alerts._calendar_loaded = False
            code, text, _ = run(["clock", "--now", "2026-11-08T18:30"])
            self.assertEqual(code, 0)
            self.assertIn("NSE OPEN, special session", text)
            self.assertIn("Today's session: 18:00 to 19:00 IST (Muhurat trading).", text)
        finally:
            os.chdir(cwd)
            alerts.SPECIAL_SESSIONS["2026-11-08"] = saved_sp
            alerts._calendar_loaded = saved

    def test_clock_shows_the_real_muhurat_timings(self):
        code, text, _ = run(["clock", "--now", "2026-11-02T10:00"])
        self.assertIn("Diwali Muhurat trading on 2026-11-08: 14:20 to 15:30 IST", text)
        code, text, _ = run(["clock", "--now", "2026-11-08T14:30"])
        self.assertEqual(code, 0)
        self.assertIn("Today's session: 14:20 to 15:30 IST (Diwali Muhurat trading).", text)

    def test_clock_shows_the_closing_auction_for_an_f_and_o_stock(self):
        code, text, _ = run(["clock", "--now", "2026-10-26T15:20", "--symbol", "RELIANCE", "--offline"])
        self.assertIn("Closing auction: F&O stocks stop continuous trading at 15:15, other stocks at 15:30", text)
        self.assertIn("RELIANCE is an F&O stock: continuous trading ends at 15:15 and has ended", text)
        code, text, _ = run(["clock", "--now", "2026-10-26T15:20", "--symbol", "AWL", "--offline"])
        self.assertIn("AWL trades to 15:30.", text)
        code, text, _ = run(["clock", "--now", "2026-10-26T10:00", "--symbol", "RELIANCE", "--offline", "--json"])
        st = json.loads(text)
        self.assertEqual((st["cas_stock"], st["continuous_close"], st["fo_close"]), (True, "15:15", "15:15"))
        # 2026-12-07: back to 15:30 for everyone, no auction line
        code, text, _ = run(["clock", "--now", "2026-12-07T10:00", "--symbol", "RELIANCE", "--offline"])
        self.assertNotIn("Closing auction", text)
        self.assertIn("RELIANCE trades to 15:30.", text)

    def test_flat_by_reminder_uses_the_default_for_the_stock_and_broker(self):
        with open(PLAN) as fh:
            plan = {k: v for k, v in json.load(fh).items() if k != "flat_by"}
        plan["date"] = "2026-10-26"
        for broker, cas, want in (("zerodha", True, "15:07"), ("dhan", True, "15:05"), ("zerodha", False, "15:10")):
            rules = alerts.rules_from_plan(dict(plan, cas_stock=cas), None, None, {"broker": broker})
            flat = [r for r in rules if r["type"] == "time"][0]
            self.assertEqual(flat["at"], want, (broker, cas))
        # on a Muhurat day the reminder for an F&O stock moves to 5 minutes before ITS end (15:15)
        rule = {"id": "flat", "type": "time", "at": "15:10", "label": "flat by", "before_close": True, "cas_stock": True}
        st = alerts.market_status(ist("2026-11-08T15:10"))
        self.assertEqual(alerts.ring_time(rule, st), datetime.time(15, 10))
        self.assertEqual(alerts.ring_time(dict(rule, cas_stock=False), st), datetime.time(15, 25))

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)

    def test_check_over_the_network_fetches_the_band_list_once(self):
        saved = (alerts._open_url, alerts._sleep, alerts.time)
        f = FakeNet([200, 200])
        alerts._open_url = f.open_url
        alerts._sleep = f.sleeps.append
        try:
            run(["add", "--rules", self.rules, "--symbol", "JPPOWER", "--circuit", "--offline", QDIR])
            cache = os.path.join(self.d, "state", "sec_list.json")
            argv = ["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--bands-cache", cache, "--now", "2026-10-26T11:00"]
            self.assertEqual(run(argv)[0], 1)
            self.assertEqual(len(f.calls), 2)                     # one quote, one sec_list.csv
            self.assertTrue(any("sec_list.csv" in u for u in f.calls))
            f.codes = [200]
            self.assertEqual(run(argv)[0], 1)
            self.assertEqual(len(f.calls), 3)                     # a second check: the quote only, the list is cached
        finally:
            alerts._open_url, alerts._sleep, alerts.time = saved


if __name__ == "__main__":
    unittest.main()
