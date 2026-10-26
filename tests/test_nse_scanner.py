import csv
import datetime
import gzip
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

scan = load("nse-scanner", "scan")

SDIR = os.path.join(FIX, "nse-scanner")
WALL = os.path.join(SDIR, "wall5m-nifty50-2026-10-23.json.gz")        # 50 real v8 5-minute charts, Fri Oct 23 2026
SPARK5 = os.path.join(SDIR, "spark-5m-nifty50-2026-10-23.json")       # the same day as a real spark answer
DAILY = os.path.join(SDIR, "spark-daily-nifty50-2026-10-26.json")     # 3 months of daily closes, fetched Oct 26 10:27 IST
DT = datetime.datetime
D = datetime.date

LATE = "2026-10-23 16:00"      # after Friday's close


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(ist_text):
    """India wall-clock text -> unix seconds (through the tool's own clock helpers)."""
    fmt = "%Y-%m-%d %H:%M:%S" if ist_text.count(":") == 2 else "%Y-%m-%d %H:%M"
    return scan.ts_of(scan.ist_to_utc(DT.strptime(ist_text, fmt)))


def bars_series(day, closes, highs=None, lows=None, start="09:15", extra=None):
    """A v8-like series of 5-min bars from `start` on `day` (YYYY-MM-DD)."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    s = {"t": t, "c": list(closes), "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
         "l": list(lows or [c - 0.1 if c is not None else None for c in closes]), "o": None,
         "v": [1000] * len(closes), "meta": {}}
    for when, c in (extra or []):
        s["t"].append(ts(when))
        for k in ("c", "h", "l"):
            s[k].append(c)
        s["v"].append(10)
    return s


def entry(sym):
    return scan.make_entry(sym, sym.title(), "EQ")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def raw_daily():
    with open(DAILY, encoding="utf-8") as fh:
        return json.load(fh)


def raw_bhav(day):
    """Independent reader of a fixture bhavcopy: {SYMBOL: {col: str}} (EQ rows only)."""
    out = {}
    with open(os.path.join(SDIR, "sec_bhavdata_full_%s102026.csv" % day), encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    hdr = [h.strip() for h in rows[0]]
    for r in rows[1:]:
        d = dict(zip(hdr, [c.strip() for c in r]))
        if d["SERIES"] == "EQ":
            out[d["SYMBOL"]] = d
    return out


class Time(unittest.TestCase):
    def test_ist_clock(self):
        self.assertEqual(scan.ts_to_ist(1792986300), DT(2026, 10, 26, 9, 15))     # TCS's first bar of Oct 26
        self.assertEqual(scan.parse_now("2026-10-23 10:15"), DT(2026, 10, 23, 4, 45))
        self.assertEqual(scan.parse_now("2026-10-23T04:45:00Z"), DT(2026, 10, 23, 4, 45))
        self.assertEqual(scan.parse_now("2026-10-23 10:15 IST"), DT(2026, 10, 23, 4, 45))
        self.assertEqual(scan.clock_label(DT(2026, 10, 23, 10, 15)), "10:15 AM")
        self.assertEqual(scan.clock_label(datetime.time(15, 30)), "3:30 PM")
        self.assertEqual(scan.clock_label(datetime.time(9, 35)), "9:35 AM")
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm_flag("25:00")

    def test_nse_calendar(self):
        self.assertEqual(scan.trading_day(D(2026, 10, 20)), (False, "NSE holiday: Dussehra"))
        self.assertEqual(scan.trading_day(D(2026, 10, 24)), (False, "weekend"))
        self.assertEqual(scan.session(D(2026, 10, 23)), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertIsNone(scan.session(D(2026, 11, 10)))     # Diwali-Balipratipada


class Spark(unittest.TestCase):
    def test_a_real_spark_answer(self):
        data = scan.parse_spark(read(DAILY))
        self.assertEqual(len(data), 50)
        self.assertIn("M&M.NS", data)                        # the & survives the round trip
        t = data["TCS.NS"]
        self.assertIsNone(t["h"])                            # spark is closes only, with no volume
        self.assertIsNone(t["v"])
        self.assertEqual(len(t["t"]), 62)
        self.assertEqual(scan.ts_to_ist(t["t"][-1]), DT(2026, 10, 26, 9, 15))
        self.assertEqual(t["c"][-1], 2532.4)                 # the live price at 10:27 IST on Oct 26
        self.assertEqual(t["meta"]["chartPreviousClose"], 2254.3)
        bars = scan.daily_closes(t, scan.parse_now("2026-10-26 09:00"))
        self.assertEqual(bars[-1], (D(2026, 10, 23), 2530.0))
        # the same answer read after Monday's close: Monday's bar is now a complete one
        bars = scan.daily_closes(t, scan.parse_now("2026-10-26 15:30"))
        self.assertEqual(bars[-1], (D(2026, 10, 26), 2532.4))
        bars = scan.daily_closes(t, scan.parse_now("2026-10-26 15:29"))
        self.assertEqual(bars[-1][0], D(2026, 10, 23))

    def test_spark_errors(self):
        too_many = '{"spark":{"result":null,"error":{"code":"Bad Request",' \
                   '"description":"Number of symbols needs to be less than or equal to 20"}}}'
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        self.assertEqual(scan.parse_spark("{}"), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')

    def test_urls(self):
        self.assertEqual(scan.spark_path(["M&M.NS", "TCS.NS", "^NSEI"], "3mo", "1d"),
                         "/v8/finance/spark?symbols=M%26M.NS,TCS.NS,%5ENSEI&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("M&M.NS", "1d", "5m"), "/v8/finance/chart/M%26M.NS?range=1d&interval=5m")

    def test_entries_use_parse_symbol(self):
        e = scan.entries_from_symbols(["reliance", "TCS.NS", "RELIANCE.BO", "M&M", "tcs"])
        self.assertEqual([(x["symbol"], x["exchange"], x["yahoo"]) for x in e],
                         [("RELIANCE", "NSE", "RELIANCE.NS"), ("TCS", "NSE", "TCS.NS"),
                          ("RELIANCE", "BSE", "RELIANCE.BO"), ("M&M", "NSE", "M&M.NS")])
        with self.assertRaises(scan.ScanError):
            scan.entries_from_symbols(["NIFTY"])
        with self.assertRaises(scan.ScanError) as cm:
            scan.entries_from_symbols(["500325"])
        self.assertIn("numeric", str(cm.exception))           # BSE numeric codes do not work on Yahoo


class Network(unittest.TestCase):
    """429s and 5xx without the network."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep, self._backoff = scan._open_url, scan.SLEEP, scan.BACKOFF
        scan.SLEEP = self.sleeps.append
        scan.BACKOFF = 0.0
        self.body = read(DAILY)
        syms = sorted(json.loads(self.body))
        self.ents = [entry(s[:-3]) for s in syms]

    def tearDown(self):
        scan._open_url, scan.SLEEP, scan.BACKOFF = self._open, self._sleep, self._backoff

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20, accept="application/json"):
            self.calls.append(url.split("/v8")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan._open_url = fake

    def test_429_falls_back_then_retries(self):
        self.answers([429, 429, 200])
        text = scan.yahoo_get("/v8/finance/spark?symbols=TCS.NS&range=3mo&interval=1d")
        self.assertEqual(text, self.body)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query2.finance.yahoo.com",
                                      "https://query1.finance.yahoo.com"])

    def test_5xx_goes_to_query1(self):
        self.answers([503, 200])
        scan.yahoo_get("/v8/finance/chart/TCS.NS?range=1d&interval=5m")
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com"])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 20)
        # 50 symbols = 3 batches of at most 20; the first answers (the whole fixture, as the fake is
        # not batch-aware), the others never do
        data, failed = scan.fetch_spark(self.ents, "3mo", "1d", 0.3)
        self.assertEqual(len(data), 20)
        self.assertEqual(len(failed), 30)
        self.assertTrue(all("did not answer" in v and "HTTP 429" in v for v in failed.values()))
        self.assertEqual(self.sleeps, [0.3, 0.3])             # a pause between batches
        hits, counts, _ = scan.scan_daily(data, self.ents, scan.parse_now(LATE), 0)
        self.assertEqual(counts["requested"], 50)
        self.assertEqual(counts["fetched"], 20)

    def test_a_missing_symbol_is_said(self):
        self.answers([])
        # Yahoo silently drops symbols it does not know: the answer simply lacks the key
        self.body = json.dumps({k: v for k, v in json.loads(self.body).items() if k != "TCS.NS"})
        data, failed = scan.fetch_spark([entry("TCS"), entry("INFY")], "3mo", "1d", 0)
        self.assertEqual(failed, {"TCS.NS": "no data from Yahoo"})
        self.assertIn("INFY.NS", data)

    def test_a_404_without_a_body_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/NOPE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)

    def test_http_text_maps_404(self):
        def fake(url, timeout=20, accept="x"):
            raise urllib.error.HTTPError(url, 404, "x", {}, io.BytesIO(b""))
        scan._open_url = fake
        with self.assertRaises(scan.NotFound):
            scan.http_text("https://archives.nseindia.com/products/content/sec_bhavdata_full_20102026.csv")


class Lists(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fetched = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def fetch(self, url):
        self.fetched.append(url)
        name = "ind_nifty50list.csv" if "ind_" in url else "EQUITY_L.csv"
        return read(os.path.join(SDIR, name))

    def test_parse_the_nifty_list(self):
        rows = scan.parse_stock_list(read(os.path.join(SDIR, "ind_nifty50list.csv")))
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0], {"symbol": "ADANIENT", "name": "Adani Enterprises Ltd.", "series": "EQ"})
        self.assertIn("M&M", [r["symbol"] for r in rows])
        with self.assertRaises(scan.ScanError):
            scan.parse_stock_list("<html>maintenance</html>")

    def test_equity_l_keeps_the_eq_series_only_for_all(self):
        text = read(os.path.join(SDIR, "EQUITY_L.csv"))
        every = scan.parse_stock_list(text)
        eq = scan.parse_stock_list(text, eq_only=True)
        self.assertEqual({r["series"] for r in every}, {"EQ", "BE", "BZ"})
        self.assertEqual({r["series"] for r in eq}, {"EQ"})
        self.assertEqual(len(every), 106)
        self.assertEqual(len(eq), 54)
        self.assertEqual([r for r in eq if r["symbol"] == "20MICRONS"][0]["name"], "20 Microns Limited")   # leading space stripped

    def test_cache_is_dated_and_reused_for_a_week(self):
        today = D(2026, 10, 26)
        rows, note = scan.get_list("nifty50", self.tmp, today, fetch=self.fetch)
        self.assertEqual((len(rows), note), (50, "list downloaded 2026-10-26"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "ind_nifty50list-2026-10-26.csv")))
        self.assertEqual(len(self.fetched), 1)
        self.assertIn("niftyindices.com/IndexConstituent/ind_nifty50list.csv", self.fetched[0])
        rows, note = scan.get_list("nifty50", self.tmp, D(2026, 11, 2), fetch=self.fetch)    # 7 days old: still fresh
        self.assertEqual(note, "list cached 2026-10-26")
        self.assertEqual(len(self.fetched), 1)
        scan.get_list("nifty50", self.tmp, D(2026, 11, 3), fetch=self.fetch)               # 8 days old: download
        self.assertEqual(len(self.fetched), 2)
        scan.get_list("nifty50", self.tmp, D(2026, 11, 3), refresh=True, fetch=self.fetch)
        self.assertEqual(len(self.fetched), 3)

    def test_a_failed_download_falls_back_to_the_old_cache(self):
        scan.get_list("nifty50", self.tmp, D(2026, 10, 1), fetch=self.fetch)

        def down(url):
            raise scan.ScanError("could not reach www.niftyindices.com")
        rows, note = scan.get_list("nifty50", self.tmp, D(2026, 10, 26), fetch=down)
        self.assertEqual(len(rows), 50)
        self.assertIn("list cached 2026-10-01 (a fresh download failed", note)
        with self.assertRaises(scan.ScanError):
            scan.get_list("nifty100", self.tmp, D(2026, 10, 26), fetch=down)

    def test_all_uses_equity_l_and_shares_its_cache_name(self):
        rows, _ = scan.get_list("all", self.tmp, D(2026, 10, 26), fetch=self.fetch)
        self.assertEqual({r["series"] for r in rows}, {"EQ"})
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "EQUITY_L-2026-10-26.csv")))
        self.assertIn("archives.nseindia.com/content/equities/EQUITY_L.csv", self.fetched[0])
        names = scan.known_names(self.tmp)
        self.assertEqual(names["20MICRONS"], ("20 Microns Limited", "EQ"))
        self.assertEqual(names["3IINFOLTD"][1], "BE")

    def test_every_index_list_has_a_url(self):
        for kind in ("nifty50", "nifty100", "nifty200", "nifty500", "midcap150", "smallcap250", "bank"):
            prefix, url, eq_only = scan.list_source(kind)
            self.assertTrue(url.startswith("https://www.niftyindices.com/IndexConstituent/ind_nifty"), kind)
            self.assertFalse(eq_only)
        self.assertEqual(scan.list_source("midcap150")[0], "ind_niftymidcap150list")
        with self.assertRaises(scan.ScanError):
            scan.list_source("nifty51")


class Bhavcopy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.asked = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def fetch(self, url):
        self.asked.append(url)
        name = url.rsplit("/", 1)[-1]
        p = os.path.join(SDIR, name)
        if not os.path.exists(p):
            raise scan.NotFound("no file " + name)
        return read(p)

    def test_parse(self):
        when, rows = scan.parse_bhav(read(os.path.join(SDIR, "sec_bhavdata_full_23102026.csv")))
        self.assertEqual(when, D(2026, 10, 23))
        self.assertEqual(rows["RELIANCE"], {"series": "EQ", "prev_close": 1184.5, "close": 1190.0, "qty": 14905641.0,
                                            "turnover_lakh": 176004.07, "trades": 241397.0,
                                            "deliv_qty": 7634419.0, "deliv_per": 51.22})
        self.assertEqual(rows["M&M"]["deliv_per"], 56.61)
        be = rows["3IINFOLTD"]                               # trade-for-trade: NSE prints '-'
        self.assertEqual((be["series"], be["deliv_qty"], be["deliv_per"]), ("BE", None, None))
        with self.assertRaises(scan.ScanError):
            scan.parse_bhav("<html>Access Denied</html>")

    def test_the_eq_series_wins(self):
        text = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, "
                "AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
                "ABC, BL, 23-Oct-2026, 10, 10, 10, 10, 10, 10, 10, 5, 1.0, 1, 5, 100.00\n"
                "ABC, EQ, 23-Oct-2026, 10, 10, 10, 10, 10, 11, 10, 500, 50.0, 100, 200, 40.00\n"
                "ABC, BL, 23-Oct-2026, 10, 10, 10, 10, 10, 10, 10, 5, 1.0, 1, 5, 100.00\n")
        _, rows = scan.parse_bhav(text)
        self.assertEqual((rows["ABC"]["series"], rows["ABC"]["close"]), ("EQ", 11.0))

    def test_walk_back_over_the_nse_calendar(self):
        def dates(now, count):
            return [d for d, _ in scan.recent_bhavs(scan.parse_now(now), count, self.tmp, SDIR)]
        self.assertEqual(dates("2026-10-24 12:00", 3), [D(2026, 10, 23), D(2026, 10, 22), D(2026, 10, 21)])  # Saturday
        self.assertEqual(dates("2026-10-23 16:00", 1), [D(2026, 10, 23)])
        self.assertEqual(dates("2026-10-23 12:00", 1), [D(2026, 10, 22)])     # today's session is not over
        # 19th, then back over the Dussehra holiday (20th is in the list: it is skipped by the calendar)
        self.assertEqual(dates("2026-10-23 16:00", 8),
                         [D(2026, 10, 23), D(2026, 10, 22), D(2026, 10, 21), D(2026, 10, 19), D(2026, 10, 16),
                          D(2026, 10, 15), D(2026, 10, 14), D(2026, 10, 13)])
        self.assertNotIn(D(2026, 10, 20), dates("2026-10-23 16:00", 8))

    def test_online_fetch_caches_and_skips_a_missing_day(self):
        now = scan.parse_now("2026-10-26 18:30")             # Monday evening: NSE has not published the 26th
        got = scan.recent_bhavs(now, 2, self.tmp, fetch=self.fetch)
        self.assertEqual([d for d, _ in got], [D(2026, 10, 23), D(2026, 10, 22)])
        self.assertEqual([u.rsplit("_", 1)[-1] for u in self.asked], ["26102026.csv", "23102026.csv", "22102026.csv"])
        self.assertIn("products/content/sec_bhavdata_full_26102026.csv", self.asked[0])
        folder = os.path.join(self.tmp, "bhav")
        self.assertEqual(sorted(os.listdir(folder)),
                         ["sec_bhavdata_full_22102026.csv", "sec_bhavdata_full_23102026.csv"])
        self.asked[:] = []
        scan.recent_bhavs(now, 2, self.tmp, fetch=self.fetch)  # cached days are not asked again (26th is retried)
        self.assertEqual(len(self.asked), 1)

    def test_old_files_are_pruned(self):
        old = scan.BHAV_KEEP
        scan.BHAV_KEEP = 2
        try:
            scan.recent_bhavs(scan.parse_now("2026-10-24 12:00"), 4, self.tmp, fetch=self.fetch)
        finally:
            scan.BHAV_KEEP = old
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, "bhav"))),
                         ["sec_bhavdata_full_22102026.csv", "sec_bhavdata_full_23102026.csv"])

    def test_pause_is_polite(self):
        sleeps, old = [], scan.SLEEP
        scan.SLEEP = sleeps.append
        try:
            scan.recent_bhavs(scan.parse_now("2026-10-24 12:00"), 2, self.tmp, pause=0.3, fetch=self.fetch)
        finally:
            scan.SLEEP = old
        self.assertEqual(sleeps, [0.3, 0.3])


class Daily(unittest.TestCase):
    # through Fri Oct 23 2026. Hand-checked below against the raw closes (test_hand_checks).
    EXPECT = {
        "INFY": ["bull_flag"], "TCS": ["breakout_20d", "bull_flag"], "BSE": ["bull_flag"],
        "HCLTECH": ["bull_flag"], "ADANIENT": ["oversold"], "JIOFIN": ["oversold"],
        "MAXHEALTH": ["oversold"], "ONGC": ["reclaim_ma20"], "TITAN": ["oversold"], "NTPC": ["oversold"],
        "NESTLEIND": ["oversold"], "BAJAJFINSV": ["oversold"],
    }

    def setUp(self):
        self.data, self.errors = scan.load_offline(DAILY)
        self.ents = [entry(k[:-3]) for k in sorted(self.data)]
        _, self.liq = scan.parse_bhav(read(os.path.join(SDIR, "sec_bhavdata_full_23102026.csv")))

    def test_setups_after_the_close(self):
        hits, counts, last = scan.scan_daily(self.data, self.ents, scan.parse_now(LATE), 0)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EXPECT)
        self.assertEqual(last, D(2026, 10, 23))
        self.assertEqual(counts, {"requested": 50, "fetched": 50, "illiquid": 0, "short": 0, "scanned": 50,
                                  "hits": 12})
        tcs = [h for h in hits if h["symbol"] == "TCS"][0]
        self.assertEqual((tcs["price"], tcs["rsi"], tcs["close_date"], tcs["exchange"], tcs["yahoo"]),
                         (2530.0, 74.1, "2026-10-23", "NSE", "TCS.NS"))

    def test_hand_checks(self):
        raw = raw_daily()
        for sym, tags in self.EXPECT.items():
            closes = [c for t, c in zip(raw[sym + ".NS"]["timestamp"], raw[sym + ".NS"]["close"])
                      if c is not None and scan.ts_to_ist(t).date() <= D(2026, 10, 23)]
            last = closes[-1]
            if "breakout_20d" in tags:
                self.assertGreater(last, max(closes[-21:-1]), sym)
            if "bull_flag" in tags:
                self.assertGreater(closes[-6] / closes[-16] - 1, 0.10, sym)
                self.assertLess((max(closes[-5:]) - min(closes[-5:])) / last, 0.05, sym)
            if "reclaim_ma20" in tags:
                self.assertLess(closes[-2], sum(closes[-21:-1]) / 20, sym)
                self.assertGreater(last, sum(closes[-20:]) / 20, sym)
            if "oversold" in tags:
                self.assertLess(self.rsi_textbook(closes), 30, sym)
        # and a chart that matched nothing really is quiet
        closes = [c for t, c in zip(raw["RELIANCE.NS"]["timestamp"], raw["RELIANCE.NS"]["close"])
                  if c is not None and scan.ts_to_ist(t).date() <= D(2026, 10, 23)]
        self.assertFalse(closes[-1] > max(closes[-21:-1]))
        self.assertTrue(30 <= self.rsi_textbook(closes))

    @staticmethod
    def rsi_textbook(closes, n=14):
        """Wilder's smoothing written the long way round, to check the tool's version."""
        ch = [b - a for a, b in zip(closes, closes[1:])]
        up = [max(c, 0) for c in ch]
        dn = [max(-c, 0) for c in ch]
        avg_u, avg_d = sum(up[:n]) / n, sum(dn[:n]) / n
        for u, d in zip(up[n:], dn[n:]):
            avg_u = (avg_u * (n - 1) + u) / n
            avg_d = (avg_d * (n - 1) + d) / n
        return 100.0 if avg_d == 0 else 100 - 100 / (1 + avg_u / avg_d)

    def test_the_in_progress_bar_is_dropped(self):
        before = scan.scan_daily(self.data, self.ents, scan.parse_now("2026-10-26 09:00"), 0)
        during = scan.scan_daily(self.data, self.ents, scan.parse_now("2026-10-26 15:29"), 0)
        self.assertEqual(before, during)
        after = scan.scan_daily(self.data, self.ents, scan.parse_now("2026-10-26 15:30"), 0)
        self.assertEqual(after[2], D(2026, 10, 26))           # Monday's bar counts once the session closes

    def test_a_holiday_bar_rule(self):
        # on a day with no session (Dussehra) nothing is in progress: its bar is complete at any hour
        s = {"t": [ts("2026-10-19 09:15"), ts("2026-10-20 09:15")], "c": [10.0, 11.0], "meta": {}}
        self.assertEqual(len(scan.daily_closes(s, scan.parse_now("2026-10-20 10:00"))), 2)
        self.assertEqual(len(scan.daily_closes(s, scan.parse_now("2026-10-19 10:00"))), 0)   # Monday's bar is in progress
        self.assertEqual(len(scan.daily_closes(s, scan.parse_now("2026-10-19 15:30"))), 1)

    def test_price_floor(self):
        hits, counts, _ = scan.scan_daily(self.data, self.ents, scan.parse_now(LATE), 1000)
        last = {k: v for k, v in
                ((e["symbol"], scan.daily_closes(self.data[e["yahoo"]], scan.parse_now(LATE))[-1][1])
                 for e in self.ents)}
        self.assertEqual(counts["illiquid"], sum(1 for v in last.values() if v < 1000))
        self.assertEqual(sorted(h["symbol"] for h in hits),
                         sorted(s for s in self.EXPECT if last[s] >= 1000))
        self.assertNotIn("NTPC", [h["symbol"] for h in hits])      # Rs 298

    def test_turnover_floor_and_ranking(self):
        floor = 50000.0
        hits, counts, _ = scan.scan_daily(self.data, self.ents, scan.parse_now(LATE), 0, floor, self.liq)
        thin = [e["symbol"] for e in self.ents if self.liq[e["symbol"]]["turnover_lakh"] < floor]
        self.assertEqual(counts["illiquid"], len(thin))
        self.assertTrue(0 < len(thin) < 50)
        self.assertTrue(all(self.liq[h["symbol"]]["turnover_lakh"] >= floor for h in hits))
        # most rupees traded first
        turns = [h["turnover_lakh"] for h in hits]
        self.assertEqual(turns, sorted(turns, reverse=True))
        self.assertEqual(hits[0]["symbol"], "INFY")
        self.assertEqual(hits[0]["volume"], int(self.liq["INFY"]["qty"]))
        # a floor without any bhavcopy cannot pass anything
        hits, counts, _ = scan.scan_daily(self.data, self.ents, scan.parse_now(LATE), 0, floor, None)
        self.assertEqual((hits, counts["illiquid"]), ([], 50))

    def test_short_and_empty_charts(self):
        s = {"t": [ts("2026-10-%02d 09:15" % d) for d in range(1, 11)], "c": [10.0] * 10, "meta": {}}
        hits, counts, last = scan.scan_daily({"X.NS": s, "E.NS": {"t": [], "c": [], "meta": {}}},
                                             [entry("X"), entry("E"), entry("GONE")], scan.parse_now("2026-10-26 09:00"),
                                             5)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["illiquid"], counts["scanned"]), (2, 1, 1, 0))
        self.assertIsNone(last)

    def test_rsi(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)    # Wilder's textbook example


class Delivery(unittest.TestCase):
    def setUp(self):
        days = scan.recent_bhavs(scan.parse_now("2026-10-24 12:00"), 8, "unused", SDIR)
        self.latest = days[0][1]
        self.hist = [r for _, r in days[1:]]
        self.ents = [scan.make_entry(s, s, "EQ") for s in sorted(self.latest) if self.latest[s]["series"] == "EQ"]

    LOOSE = dict(per_rise=3.0, qty_mult=1.2, min_deliv_per=30.0)

    def test_the_real_day(self):
        hits, counts = scan.scan_delivery(self.ents, self.latest, self.hist, **self.LOOSE)
        self.assertEqual(sorted(h["symbol"] for h in hits), ["360ONE", "ADANIENT", "BAJAJ-AUTO", "HINDALCO", "MAXHEALTH"])
        self.assertEqual(counts, {"requested": 54, "in_bhavcopy": 54, "no_delivery_data": 0, "illiquid": 0,
                                  "short": 0, "scanned": 54, "hits": 5})
        adani = [h for h in hits if h["symbol"] == "ADANIENT"][0]
        self.assertEqual((adani["price"], adani["chg_pct"], adani["deliv_per"], adani["deliv_qty"]),
                         (2477.0, -2.86, 40.92, 731584))
        self.assertEqual(adani["setups"], ["delivery"])
        self.assertEqual(adani["history_days"], 7)

    def test_the_defaults_find_nothing_that_day(self):
        hits, counts = scan.scan_delivery(self.ents, self.latest, self.hist)
        self.assertEqual((hits, counts["scanned"]), ([], 54))

    def test_hand_check_one_stock(self):
        # ADANIENT's own average over the 7 earlier bhavcopies, from the raw CSV files
        pers, qtys = [], []
        for day in ("22", "21", "19", "16", "15", "14", "13"):
            r = raw_bhav(day)["ADANIENT"]
            pers.append(float(r["DELIV_PER"]))
            qtys.append(float(r["DELIV_QTY"]))
        today = raw_bhav("23")["ADANIENT"]
        hits, _ = scan.scan_delivery(self.ents, self.latest, self.hist, **self.LOOSE)
        h = [x for x in hits if x["symbol"] == "ADANIENT"][0]
        self.assertAlmostEqual(h["avg_deliv_per"], sum(pers) / 7, places=2)
        self.assertEqual(h["avg_deliv_qty"], int(round(sum(qtys) / 7)))
        self.assertAlmostEqual(h["qty_x"], float(today["DELIV_QTY"]) / (sum(qtys) / 7), places=2)
        self.assertGreaterEqual(h["deliv_per"], h["avg_deliv_per"] + 3)

    def test_each_rule_alone_keeps_a_stock_out(self):
        def one(**kw):
            hits, _ = scan.scan_delivery(self.ents, self.latest, self.hist, **dict(self.LOOSE, **kw))
            return {h["symbol"] for h in hits}
        base = one()
        self.assertIn("ADANIENT", base)
        self.assertNotIn("ADANIENT", one(per_rise=9.0))           # +7.5 points only
        self.assertNotIn("ADANIENT", one(qty_mult=2.5))           # 2.16x only
        self.assertNotIn("ADANIENT", one(min_deliv_per=45.0))     # 40.9 % only
        self.assertEqual(one(qty_mult=2.0), {"360ONE", "HINDALCO", "ADANIENT"})

    def test_liquidity_and_history_rules(self):
        hits, counts = scan.scan_delivery(self.ents, self.latest, self.hist, min_price=1000.0, **self.LOOSE)
        self.assertNotIn("HINDALCO", [h["symbol"] for h in hits])  # under Rs 1000
        self.assertEqual(counts["illiquid"], sum(1 for e in self.ents if self.latest[e["symbol"]]["close"] < 1000))
        hits, counts = scan.scan_delivery(self.ents, self.latest, self.hist, min_turnover=1e9, **self.LOOSE)
        self.assertEqual((hits, counts["illiquid"]), ([], 54))
        hits, counts = scan.scan_delivery(self.ents, self.latest, self.hist[:2], **self.LOOSE)
        self.assertEqual((hits, counts["short"]), ([], 54))       # 2 earlier days < 3 needed
        hits, counts = scan.scan_delivery(self.ents, self.latest, self.hist[:3], **self.LOOSE)
        self.assertEqual(counts["scanned"], 54)

    def test_be_series_and_missing_delivery_data(self):
        self.assertEqual(self.latest["AARNAV"]["series"], "BE")
        ents = [scan.make_entry("AARNAV", "Aarnav", "BE"), scan.make_entry("GONE", "Gone", "EQ")]
        hits, counts = scan.scan_delivery(ents, self.latest, self.hist, **self.LOOSE)
        self.assertEqual((hits, counts["in_bhavcopy"]), ([], 0))  # BE is trade-for-trade, not scanned
        row = dict(self.latest["TCS"], deliv_per=None, deliv_qty=None)
        hits, counts = scan.scan_delivery([entry("TCS")], {"TCS": row}, self.hist, **self.LOOSE)
        self.assertEqual((hits, counts["no_delivery_data"]), ([], 1))

    def test_hits_are_ordered_by_rupees_delivered(self):
        hits, _ = scan.scan_delivery(self.ents, self.latest, self.hist, per_rise=-100.0, qty_mult=0.0,
                                     min_deliv_per=0.0)
        vals = [h["deliv_value_lakh"] for h in hits]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertEqual(len(hits), 54)
        top = hits[0]
        self.assertAlmostEqual(top["deliv_value_lakh"], top["deliv_qty"] * top["price"] / 1e5, delta=0.01)


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(WALL)
        cls.ents = [entry(k[:-3]) for k in sorted(cls.data)]
        _, cls.liq = scan.parse_bhav(read(os.path.join(SDIR, "sec_bhavdata_full_23102026.csv")))

    def scan_at(self, now, min_price=0, ents=None, or_minutes=15, **kw):
        day, cutoff = scan.orb_window(scan.parse_now(now), None, or_minutes, datetime.time(14, 45))
        return scan.scan_orb(self.data, ents or self.ents, day, cutoff, or_minutes, min_price, **kw)

    def test_hits_through_the_day(self):
        self.assertEqual(len(self.data), 50)
        hits, counts, failed = self.scan_at("2026-10-23 09:35")
        self.assertEqual((hits, counts["scanned"], counts["candidates"]), ([], 50, 1))
        hits, counts, failed = self.scan_at("2026-10-23 10:15")
        self.assertEqual(hits, [dict(
            symbol="ONGC", exchange="NSE", yahoo="ONGC.NS", series="EQ", name="Ongc", setups=["orb"], price=225.74,
            at="10:05 AM", bar_start="10:00", bar_index=9, or_high=225.62, or_low=224.61, pct_over=0.05)])
        self.assertEqual((counts["scanned"], counts["unusable"], counts["candidates"]), (50, 0, 11))
        hits, counts, failed = self.scan_at("2026-10-23 14:00")
        self.assertEqual([(h["symbol"], h["at"]) for h in hits],
                         [("ONGC", "10:05 AM"), ("MAXHEALTH", "11:05 AM"), ("ETERNAL", "11:15 AM"),
                          ("HDFCBANK", "1:45 PM")])
        self.assertEqual(counts["hits"], 4)

    def test_hand_check_one_breakout(self):
        raw = json.loads(scan._read_maybe_gz(WALL))["ONGC"]["chart"]["result"][0]
        q = raw["indicators"]["quote"][0]
        stamps = [scan.ts_to_ist(t).strftime("%H:%M") for t in raw["timestamp"]]
        self.assertEqual(stamps[0], "09:15")
        rng_high = max(q["high"][:3])                      # the 09:15, 09:20 and 09:25 bars
        self.assertAlmostEqual(rng_high, 225.62, places=2)
        first = next(i for i in range(3, len(q["close"])) if q["close"][i] > rng_high)
        self.assertEqual(stamps[first], "10:00")
        self.assertAlmostEqual(q["close"][first], 225.74, places=2)

    def test_price_floor(self):
        hits, counts, _ = self.scan_at("2026-10-23 14:00", min_price=500)
        self.assertEqual([h["symbol"] for h in hits], ["MAXHEALTH", "HDFCBANK"])     # ONGC 225 and ETERNAL 309 drop out
        self.assertEqual(counts["illiquid"], sum(1 for e in self.ents
                                                   if scan.session_bars(self.data[e["yahoo"]], D(2026, 10, 23),
                                                                        DT(2026, 10, 23, 14, 0))[-1]["c"] < 500))

    def test_turnover_floor(self):
        hits, counts, _ = self.scan_at("2026-10-23 14:00", min_turnover=30000.0, liquidity=self.liq)
        want = [h for h in ("ONGC", "MAXHEALTH", "ETERNAL", "HDFCBANK") if self.liq[h]["turnover_lakh"] >= 30000]
        self.assertEqual([h["symbol"] for h in hits], want)
        self.assertLess(len(want), 4)

    def test_in_progress_bar(self):
        hits, _, _ = self.scan_at("2026-10-23 10:04", ents=[entry("ONGC")])
        self.assertEqual(hits, [])                         # the 10:00 bar is not done until 10:05
        hits, _, _ = self.scan_at("2026-10-23 10:05", ents=[entry("ONGC")])
        self.assertEqual(hits[0]["at"], "10:05 AM")

    def test_the_live_tick_is_dropped(self):
        s = bars_series("2026-10-27", [100, 101, 100.5, 100.8, 101.2, 100.9, 101.5], extra=[("2026-10-27 09:49:45", 120)])
        cut = DT(2026, 10, 27, 9, 59)
        bars = scan.session_bars(s, D(2026, 10, 27), cut)
        self.assertEqual(len(bars), 7)                     # 09:15 .. 09:45; the 09:49:45 tick is off the grid
        self.assertEqual(bars[-1]["c"], 101.5)
        self.assertTrue(scan.usable(bars, D(2026, 10, 27), DT(2026, 10, 27, 9, 50), 3))

    def test_stage_one_is_a_necessary_condition(self):
        day = D(2026, 10, 23)
        for hh in ("09:35", "10:15", "11:30", "13:00", "14:30"):
            cut = DT(2026, 10, 23, int(hh[:2]), int(hh[3:]))
            for sym, s in self.data.items():
                bars = scan.session_bars(s, day, cut)
                if scan.usable(bars, day, cut, 3) and scan.orb_hit(bars, 3):
                    self.assertTrue(scan.orb_candidate(bars, 3), "%s at %s" % (sym, hh))

    def test_confirm_uses_the_v8_highs(self):
        spark = scan.parse_spark(read(SPARK5))
        self.assertEqual(len(spark), 50)
        asked = []

        def confirm(e):
            asked.append(e["symbol"])
            return self.data[e["yahoo"]]
        day, cut = D(2026, 10, 23), DT(2026, 10, 23, 14, 0)
        hits, counts, failed = scan.scan_orb(spark, self.ents, day, cut, 15, 0, 0, None, confirm)
        self.assertEqual([h["symbol"] for h in hits], ["ONGC", "MAXHEALTH", "ETERNAL", "HDFCBANK"])
        self.assertEqual(counts["candidates"], len(asked))
        self.assertEqual((counts["candidates"], counts["gappy"]), (32, 17))   # 15 broke the range on closes, 17 have gaps
        self.assertLess(len(asked), 50)                    # stage 1 skipped most charts
        # spark alone has no highs and lows: nothing can be decided without stage 2
        hits, counts, _ = scan.scan_orb(spark, self.ents, day, cut, 15, 0, 0, None, None)
        self.assertEqual(hits, [])

        def broken(e):
            raise scan.ScanError("Yahoo did not answer (HTTP 429 on query2 and query1)")
        hits, counts, failed = scan.scan_orb(spark, [entry("ONGC"), entry("TCS")], day, cut, 15, 0, 0, None, broken)
        self.assertEqual(hits, [])
        self.assertIn("429", failed["ONGC.NS"])

    def test_stage_two_cap_takes_the_most_traded(self):
        spark = scan.parse_spark(read(SPARK5))
        asked = []

        def confirm(e):
            asked.append(e["symbol"])
            return self.data[e["yahoo"]]
        day, cut = D(2026, 10, 23), DT(2026, 10, 23, 14, 0)
        hits_all, counts_all, _ = scan.scan_orb(spark, self.ents, day, cut, 15, 0, 0, self.liq, confirm)
        cands = list(asked)
        asked[:] = []
        hits, counts, _ = scan.scan_orb(spark, self.ents, day, cut, 15, 0, 0, self.liq, confirm, max_confirm=3)
        self.assertEqual((counts["confirmed"], counts["unconfirmed"]), (3, 29))
        by_turnover = sorted(cands, key=lambda s: -self.liq[s]["turnover_lakh"])[:3]
        self.assertEqual(sorted(asked), sorted(by_turnover))
        for h in hits:
            self.assertIn(h, hits_all)
        # no bhavcopy: the strongest close goes first, and the rule still holds
        asked[:] = []
        scan.scan_orb(spark, self.ents, day, cut, 15, 0, 0, None, confirm, max_confirm=3)
        self.assertEqual(len(asked), 3)

    def test_a_missing_bar_makes_a_chart_unusable(self):
        s = bars_series("2026-10-27", [10, 10.2, 10.1, None, 10.5, 10.6, 10.7, 10.8, 10.9])
        cut = DT(2026, 10, 27, 10, 0)
        bars = scan.session_bars(s, D(2026, 10, 27), cut)
        self.assertFalse(scan.usable(bars, D(2026, 10, 27), cut, 3))

    def test_a_30_minute_range(self):
        closes = [100, 101, 100.5, 100.8, 101.2, 100.9, 101.5] + [101.0] * 40
        s = bars_series("2026-10-27", closes, highs=[c + 0.2 for c in closes])
        hits, counts, _ = scan.scan_orb({"X.NS": s}, [entry("X")], D(2026, 10, 27), DT(2026, 10, 27, 12, 0), 30, 0)
        self.assertEqual(counts["scanned"], 1)
        self.assertEqual(hits[0]["at"], "9:50 AM")          # range 9:15 to 9:45 (high 101.4); the 9:45 bar closes 101.5
        self.assertEqual(hits[0]["or_high"], 101.4)
        bars = scan.session_bars(s, D(2026, 10, 27), DT(2026, 10, 27, 11, 59))
        self.assertEqual(len(bars), 32)                    # 9:15 to 11:50 done at 11:59

    def test_a_muhurat_session(self):
        old = dict(scan.SPECIAL_SESSIONS)
        scan.SPECIAL_SESSIONS["2026-11-08"] = {"open": datetime.time(18, 0), "close": datetime.time(19, 0),
                                               "name": "Muhurat trading"}
        try:
            until = datetime.time(14, 45)                   # the normal no_entries_after does not apply
            day, cut = scan.orb_window(scan.parse_now("2026-11-08 18:40"), None, 15, until)
            self.assertEqual((day, cut), (D(2026, 11, 8), DT(2026, 11, 8, 18, 40)))
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now("2026-11-08 18:50"), None, 15, until)   # close - 15 min is 18:45
            self.assertIn("expired", str(cm.exception))
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now("2026-11-08 18:10"), None, 15, until)
            self.assertIn("Run orb again after 6:20 PM", str(cm.exception))
            closes = [100, 101, 100.5, 100.8, 101.2, 100.9, 101.5]
            s = bars_series("2026-11-08", closes, start="18:00")
            bars = scan.session_bars(s, D(2026, 11, 8), DT(2026, 11, 8, 18, 40))
            self.assertEqual(len(bars), 7)
            self.assertTrue(scan.usable(bars, D(2026, 11, 8), DT(2026, 11, 8, 18, 35), 3))   # 18:00 .. 18:30, all there
            self.assertEqual(scan.slot(D(2026, 11, 8), 0), DT(2026, 11, 8, 18, 0))
        finally:
            scan.SPECIAL_SESSIONS.clear()
            scan.SPECIAL_SESSIONS.update(old)

    def test_window_messages(self):
        until = datetime.time(14, 45)

        def msg(now, at=None, until=until):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("weekend", msg("2026-10-24 10:15"))
        self.assertIn("Dussehra", msg("2026-10-20 10:15"))
        self.assertIn("Run orb again after 9:35 AM", msg("2026-10-23 09:20"))
        self.assertIn("Run orb again after 9:35 AM", msg("2026-10-23 09:32"))
        self.assertIn("runs 9:15 AM to 9:30 AM", msg("2026-10-23 09:20"))
        self.assertIn("expired", msg("2026-10-23 15:00"))
        self.assertIn("expired", msg("2026-10-23 11:30", until=datetime.time(11, 0)))
        self.assertIn("closed at 3:30 PM", msg("2026-10-23 16:30"))
        self.assertIn("later than now", msg("2026-10-23 10:00", at=datetime.time(10, 15)))
        self.assertIn("after the 3:30 PM close", msg("2026-10-23 17:00", at=datetime.time(16, 0)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-23 16:30"), datetime.time(10, 15), 15, until),
                         (D(2026, 10, 23), DT(2026, 10, 23, 10, 15)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-23 09:35"), None, 15, until)[1],
                         DT(2026, 10, 23, 9, 35))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-10-23 10:15"), None, 7, until)


class ClosingAuction(unittest.TestCase):
    """v1.1: F&O ("CAS") stocks stop continuous trading at 15:15 from 2026-08-03 to 2026-12-06,
    so a chart of theirs is complete with no bars after 15:10."""

    def day_series(self, day, start="09:15", n=72):
        closes = [100, 101, 100.5, 100.8, 101.2] + [101.5 + 0.01 * i for i in range(n - 5)]
        return bars_series(day, closes, highs=[c + 0.2 for c in closes], start=start)

    def scan(self, day, cutoff, series, cas, syms=("RELIANCE", "AWL")):
        ents = [entry(s) for s in syms]
        data = {e["yahoo"]: series for e in ents}
        return scan.scan_orb(data, ents, day, cutoff, 15, 0, 0, None, None, cas_stocks=cas)

    def test_cas_vs_non_cas_stock_on_2026_10_26(self):
        day, cut = D(2026, 10, 26), DT(2026, 10, 26, 15, 20)
        s = self.day_series("2026-10-26")                    # 9:15 .. 15:10: the last bar closes at 15:15
        self.assertEqual(len(s["t"]), 72)
        hits, counts, _ = self.scan(day, cut, s, {"RELIANCE"})
        self.assertEqual((counts["scanned"], counts["unusable"]), (1, 1))      # RELIANCE complete, AWL missing 2 bars
        self.assertEqual([h["symbol"] for h in hits], ["RELIANCE"] if hits else [])
        # without closing-auction knowledge the old rule applies: nobody is complete at 15:20
        _, counts, _ = self.scan(day, cut, s, None)
        self.assertEqual((counts["scanned"], counts["unusable"]), (0, 2))
        # the list could not be read: every stock is treated as F&O, the cautious end
        _, counts, _ = self.scan(day, cut, s, "all")
        self.assertEqual((counts["scanned"], counts["unusable"]), (2, 0))
        self.assertEqual(scan.stock_close(day, entry("RELIANCE"), {"RELIANCE"}), datetime.time(15, 15))
        self.assertEqual(scan.stock_close(day, entry("AWL"), {"RELIANCE"}), datetime.time(15, 30))

    def test_the_2026_12_07_switch_back_to_1530(self):
        day, cut = D(2026, 12, 7), DT(2026, 12, 7, 15, 20)
        s = self.day_series("2026-12-07")
        _, counts, _ = self.scan(day, cut, s, {"RELIANCE"})
        self.assertEqual((counts["scanned"], counts["unusable"]), (0, 2))      # RELIANCE trades to 15:30 again
        self.assertEqual(scan.stock_close(day, entry("RELIANCE"), {"RELIANCE"}), datetime.time(15, 30))
        full = self.day_series("2026-12-07", n=74)
        _, counts, _ = self.scan(day, cut, full, {"RELIANCE"})
        self.assertEqual((counts["scanned"], counts["unusable"]), (2, 0))

    def test_the_built_in_muhurat_session_2026_11_08(self):
        day, cut = D(2026, 11, 8), DT(2026, 11, 8, 15, 20)
        s = self.day_series("2026-11-08", start="14:20", n=11)                  # 14:20 .. 15:10
        self.assertEqual(scan.session(day), (datetime.time(14, 20), datetime.time(15, 30)))
        self.assertEqual(scan.slot(day, 0), DT(2026, 11, 8, 14, 20))
        _, counts, _ = self.scan(day, cut, s, {"RELIANCE"})
        self.assertEqual((counts["scanned"], counts["unusable"]), (1, 1))      # F&O stock ends at 15:15, AWL at 15:30
        self.assertEqual(scan.stock_close(day, entry("RELIANCE"), {"RELIANCE"}), datetime.time(15, 15))
        self.assertEqual(scan.stock_close(day, entry("AWL"), {"RELIANCE"}), datetime.time(15, 30))

    def test_orb_command_reads_the_fo_list_and_says_so(self):
        d = tempfile.mkdtemp()
        try:
            base = ["--lists", SDIR, "--universe", "nifty50", "--state-dir", os.path.join(d, "nse", "state")]
            code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-23 14:00", "--min-price", "0",
                                 "--no-write", "--config", os.path.join(d, "none.json")] + base)
            self.assertEqual(code, 0)
            self.assertIn("Closing auction: F&O stocks stop continuous trading at 3:15 PM, not 3:30 PM", text)
            code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-23 14:00", "--min-price", "0",
                                 "--no-write", "--json", "--config", os.path.join(d, "none.json")] + base)
            self.assertIn("3:15 PM", json.loads(text)["closing_auction"])
            # a given list wins; a missing one is an error
            code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-23 14:00", "--no-write",
                                "--fo-lots", os.path.join(d, "missing.csv"),
                                "--config", os.path.join(d, "none.json")] + base)
            self.assertEqual(code, 1)
            self.assertIn("no F&O list", err)
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.n = os.path.join(self.d, "nse")
        self.wl = os.path.join(self.n, "watchlist.json")
        self.base = ["--lists", SDIR, "--universe", "nifty50", "--state-dir", os.path.join(self.n, "state")]

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_command(self):
        out = os.path.join(self.n, "universe.txt")
        code, text, _ = run(["universe", "--universe", "nifty50", "--lists", SDIR, "--out", out])
        self.assertEqual(code, 0)
        self.assertIn("nifty50: 50 stocks", text)
        lines = read(out).splitlines()
        self.assertEqual(len(lines), 50)
        self.assertIn("M&M|Mahindra & Mahindra Ltd.", lines)
        code, text, _ = run(["universe", "--universe", "all", "--lists", SDIR, "--json"])
        self.assertEqual(json.loads(text)["series"], {"EQ": 54})
        code, _, err = run(["universe", "--universe", "nifty51", "--lists", SDIR])
        self.assertEqual(code, 1)
        self.assertIn("--universe is one of", err)

    def test_the_universe_is_downloaded_then_cached(self):
        calls = []

        def fake(url, timeout=60):
            calls.append(url)
            return read(os.path.join(SDIR, "ind_nifty50list.csv"))
        old = scan.http_text
        scan.http_text = fake
        state = os.path.join(self.n, "state")
        try:
            code, text, _ = run(["universe", "--universe", "nifty50", "--state-dir", state, "--now", "2026-10-26 10:00"])
            self.assertIn("list downloaded 2026-10-26", text)
            code, text, _ = run(["universe", "--universe", "nifty50", "--state-dir", state, "--now", "2026-10-28 10:00"])
            self.assertIn("list cached 2026-10-26", text)
        finally:
            scan.http_text = old
        self.assertEqual(len(calls), 1)
        self.assertTrue(os.path.exists(os.path.join(state, "ind_nifty50list-2026-10-26.csv")))

    def test_daily_end_to_end(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", LATE, "--out", self.wl] + self.base)
        self.assertEqual(code, 0)
        self.assertIn("Daily scan, Fri 2026-10-23 16:00 IST, closes through 2026-10-23", text)
        self.assertIn("Fetched 50 of 50 charts. 50 passed the liquidity filter (close ₹10+); 50 scanned.", text)
        self.assertIn("12 charts matched a setup: 1 20-day closing-high breakout, 4 bull flag, 1 reclaimed the "
                      "20-day average, 7 oversold (RSI under 30).", text)
        self.assertIn("  TCS             ₹2,530.00  RSI  74.1  20-day closing-high breakout, bull flag", text)
        self.assertIn("not a list of stocks to buy", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned",
                                              "failed", "source", "liquidity_from")},
                         {"created_by": "nse-scanner", "created_at": "2026-10-23T10:30:00Z", "mode": "daily",
                          "as_of": "2026-10-23 close", "universe": 50, "scanned": 50, "failed": 0,
                          "source": "yahoo", "liquidity_from": "bhavcopy 2026-10-23"})
        self.assertEqual(len(doc["hits"]), 12)
        self.assertEqual(doc["hits"][1], {
            "symbol": "TCS", "exchange": "NSE", "yahoo": "TCS.NS", "series": "EQ",
            "name": "Tata Consultancy Services Ltd.", "setups": ["breakout_20d", "bull_flag"], "price": 2530.0,
            "close_date": "2026-10-23", "rsi": 74.1, "volume": 6857942, "turnover_lakh": 176079.81})

    def test_daily_filters_and_json(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", LATE, "--json", "--no-write",
                             "--min-turnover-lakh", "50000", "--min-price", "1000"] + self.base)
        out = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(out["filters"], {"min_price": 1000.0, "min_turnover_lakh": 50000.0})
        self.assertEqual(out["counts"]["scanned"] + out["counts"]["illiquid"], 50)
        self.assertTrue(all(h["price"] >= 1000 and h["turnover_lakh"] >= 50000 for h in out["hits"]))
        self.assertEqual(out["note"], "A scan is a list of charts that match a rule, not a list of stocks to buy.")
        self.assertFalse(os.path.exists(self.wl))

    def test_orb_end_to_end(self):
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-23 14:00", "--min-price", "0",
                             "--out", self.wl, "--config", os.path.join(self.d, "none.json")] + self.base)
        self.assertEqual(code, 0)
        self.assertIn("Opening-range breakout scan, Fri 2026-10-23 2:00 PM IST: 15-minute range (9:15 AM to 9:30 AM)", text)
        self.assertIn("4 of 50 charts closed above their opening-range high by 2:00 PM:", text)
        self.assertIn("  10:05 AM  ONGC              ₹225.74  range 224.61 to 225.62  +0.05% over", text)
        self.assertIn("not a list of stocks to buy", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned",
                                              "source", "or_minutes", "interval")},
                         {"created_by": "nse-scanner", "created_at": "2026-10-23T08:30:00Z", "mode": "orb",
                          "as_of": "2026-10-23 14:00 IST", "universe": 50, "scanned": 50, "source": "yahoo",
                          "or_minutes": 15, "interval": "5m"})
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["ONGC", "MAXHEALTH", "ETERNAL", "HDFCBANK"])
        # the hits read back as a watchlist, cut with --at instead of --now (an offline replay needs no clock)
        code, text, _ = run(["orb", "--offline", WALL, "--at", "14:00", "--watchlist", self.wl, "--min-price", "0",
                             "--json", "--no-write", "--lists", SDIR, "--state-dir", self.n])
        out = json.loads(text)
        self.assertEqual((code, out["universe"], out["scanned"], len(out["hits"])), (0, 4, 4, 4))

    def test_orb_over_the_network_path_says_partial(self):
        """The networked path with a fake Yahoo that answers from the Oct 23 fixtures."""
        spark = json.loads(read(SPARK5))
        bundle = json.loads(scan._read_maybe_gz(WALL))
        calls = []

        def get(path):
            calls.append(path)
            if path.startswith("/v8/finance/spark"):
                asked = path.split("symbols=")[1].split("&")[0].split(",")
                names = [scan.urllib.parse.unquote(a) for a in asked]
                return json.dumps({k: spark[k] for k in names if k in spark})
            sym = scan.urllib.parse.unquote(path.split("/chart/")[1].split("?")[0])[:-3]
            return json.dumps(bundle[sym])
        old = (scan.yahoo_get, scan.SLEEP, scan.utc_now)
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.utc_now = lambda: scan.parse_now("2026-10-23 14:05")        # the machine clock, pinned
        try:
            code, text, _ = run(["orb", "--now", "2026-10-23 14:00", "--min-price", "0", "--max-confirm", "5",
                                 "--no-write", "--config", os.path.join(self.d, "none.json")] + self.base)
        finally:
            scan.yahoo_get, scan.SLEEP, scan.utc_now = old
        self.assertIn("50 usable; 32 candidates, 5 confirmed", text)
        self.assertIn("17 of the candidates have 5-minute bars missing in Yahoo's spark answer", text)
        self.assertIn("PARTIAL: 27 more candidates were not checked", text)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/finance/chart/")), 5)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/finance/spark")), 3)    # 50 symbols, 20 a request
        self.assertTrue(all("range=1d&interval=5m" in c for c in calls))
        self.assertTrue(any("M%26M.NS" in c for c in calls))                              # the & is escaped
        self.assertNotIn("Traceback", text)

    def test_delivery_end_to_end(self):
        code, text, _ = run(["delivery", "--now", "2026-10-24 12:00", "--out", self.wl, "--per-rise", "3",
                             "--qty-mult", "1.2", "--min-delivery-pct", "30"] + self.base)
        self.assertEqual(code, 0)
        self.assertIn("Delivery scan, bhavcopy of Fri 2026-10-23, against the average of the 7 earlier trading days "
                      "(2026-10-13 to 2026-10-22).", text)
        self.assertIn("50 of 50 stocks are in the bhavcopy (EQ series); 50 scanned", text)
        self.assertIn("4 stocks with delivery well above its own average", text)
        self.assertIn("ADANIENT        ₹2,477.00   -2.86%   40.9 (avg  33.4)      731,584 ( 2.2x avg)  ₹181.21 crore", text)
        self.assertIn("a sign of holding, not of direction", text)
        doc = self.read(self.wl)
        self.assertEqual((doc["mode"], doc["as_of"], doc["source"], doc["universe"], doc["scanned"]),
                         ("delivery", "2026-10-23 close", "nse bhavcopy", 50, 50))
        self.assertEqual(doc["history"][0], "2026-10-22")
        self.assertEqual(len(doc["history"]), 7)
        self.assertEqual(sorted(h["symbol"] for h in doc["hits"]), ["ADANIENT", "BAJAJ-AUTO", "HINDALCO", "MAXHEALTH"])
        hit = [h for h in doc["hits"] if h["symbol"] == "ADANIENT"][0]
        self.assertEqual(hit, {
            "symbol": "ADANIENT", "exchange": "NSE", "yahoo": "ADANIENT.NS", "series": "EQ",
            "name": "Adani Enterprises Ltd.", "setups": ["delivery"], "price": 2477.0, "chg_pct": -2.86,
            "deliv_per": 40.92, "avg_deliv_per": hit["avg_deliv_per"], "deliv_qty": 731584,
            "avg_deliv_qty": hit["avg_deliv_qty"], "qty_x": 2.16, "turnover_lakh": hit["turnover_lakh"],
            "deliv_value_lakh": hit["deliv_value_lakh"], "history_days": 7})
        # the delivery watchlist feeds the other skills like any other
        self.assertEqual(scan.symbols_from_file(self.wl), [h["symbol"] for h in doc["hits"]])

    def test_delivery_defaults_and_errors(self):
        code, text, _ = run(["delivery", "--now", "2026-10-24 12:00", "--out", self.wl] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("No stock stood out.", text)
        self.assertEqual(self.read(self.wl)["hits"], [])           # the empty list is written, like the US scans
        code, _, err = run(["delivery", "--now", "2026-10-24 12:00", "--history", "99", "--no-write"] + self.base)
        self.assertIn("--history wants", err)
        code, _, err = run(["delivery", "--now", "2026-10-24 12:00", "--history", "2", "--min-history", "3",
                            "--no-write"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("only 2 earlier bhavcopies found, 3 needed", err)
        empty = os.path.join(self.d, "empty")
        os.makedirs(empty)
        shutil.copy(os.path.join(SDIR, "ind_nifty50list.csv"), empty)
        code, _, err = run(["delivery", "--now", "2026-10-24 12:00", "--lists", empty, "--universe", "nifty50",
                            "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("no NSE bhavcopy found", err)
        self.assertNotIn("Traceback", err)

    def test_plain_messages_and_exit_codes(self):
        cfg = os.path.join(self.d, "none.json")
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-24 10:15", "--out", self.wl, "--config", cfg]
                           + self.base)
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-10-24 (weekend)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-23 09:20", "--config", cfg] + self.base)
        self.assertIn("after 9:35 AM", err)
        cfgfile = os.path.join(self.d, "config.json")
        with open(cfgfile, "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-23 10:15", "--config", cfgfile] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("expired", err)
        code, _, err = run(["orb", "--offline", WALL, "--config", cfg] + self.base)
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-23 10:15", "--symbols", "TCS", "--config", cfg,
                             "--out", self.wl])
        self.assertEqual(code, 1)                           # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 10:15 AM", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        code, _, err = run(["daily", "--universe-file", os.path.join(self.d, "nope.txt"), "--now", LATE])
        self.assertEqual(code, 1)
        self.assertIn("no file at", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertIn("no offline file", err)
        empty = os.path.join(self.d, "empty.json")
        with open(empty, "w") as fh:
            fh.write("{}")
        code, _, err = run(["daily", "--offline", empty])
        self.assertEqual(code, 1)
        self.assertIn("no symbols to scan", err)

    def test_the_calendar_covers_2026_only(self):
        code, _, err = run(["daily", "--offline", DAILY, "--now", "2027-01-05 20:00", "--no-write",
                            "--symbols", "TCS"])
        self.assertIn("note: The holiday list covers 2026 only", err)

    def test_offline_folder_of_v8_files(self):
        folder = os.path.join(self.d, "charts")
        os.makedirs(folder)
        bundle = json.loads(scan._read_maybe_gz(WALL))
        for sym in ("ONGC", "TCS", "HDFCBANK"):
            with open(os.path.join(folder, sym + ".json"), "w") as fh:
                json.dump(bundle[sym], fh)
        with open(os.path.join(folder, "ZZZZQX.json"), "w") as fh:
            json.dump({"chart": {"result": None, "error": {"code": "Not Found",
                                                           "description": "No data found, symbol may be delisted"}}}, fh)
        code, text, _ = run(["orb", "--offline", folder, "--now", "2026-10-23 14:00", "--no-write", "--json",
                             "--min-price", "0", "--lists", SDIR, "--state-dir", self.n])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (4, 3, 1))
        self.assertEqual([h["symbol"] for h in out["hits"]], ["ONGC", "HDFCBANK"])
        self.assertEqual(code, 0)

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w") as fh:
            fh.write("# my list\nTCS|Tata Consultancy Services\nongc\n\nTCS\nRELIANCE.BO\n")
        self.assertEqual(scan.symbols_from_file(txt), ["TCS", "ONGC", "TCS", "RELIANCE.BO"])
        js = os.path.join(self.d, "wl.json")
        with open(js, "w") as fh:
            json.dump({"hits": [{"symbol": "tcs"}, {"symbol": "ONGC"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["TCS", "ONGC"])
        code, text, _ = run(["daily", "--offline", DAILY, "--watchlist", txt, "--now", LATE, "--json", "--no-write",
                             "--lists", SDIR])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (3, 2, 1))      # RELIANCE.BO has no chart
        self.assertEqual([h["symbol"] for h in out["hits"]], ["TCS", "ONGC"])
        self.assertEqual(out["hits"][0]["series"], "EQ")                                  # looked up in EQUITY_L
        code, text, _ = run(["daily", "--offline", DAILY, "--limit", "3", "--now", LATE, "--json", "--no-write",
                             "--lists", SDIR])
        self.assertEqual(json.loads(text)["universe"], 3)
        code, text, _ = run(["daily", "--offline", DAILY, "--symbols", "TCS", "--now", LATE, "--json", "--no-write"])
        hit = json.loads(text)["hits"][0]
        self.assertIsNone(hit["series"])                  # no EQUITY_L at hand: unknown stays unknown, not "EQ"
        self.assertIsNone(json.loads(text)["hits"][0].get("volume"))


class Guards(unittest.TestCase):
    def test_no_us_leftovers_and_no_protected_endpoints(self):
        src = read(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nse-scanner", "scan.py"))
        for bad in ("NYSE", "New York", "Eastern", "nasdaqtrader", "Alpaca", "trader/", "www.nseindia.com",
                    "api.bseindia.com"):
            self.assertNotIn(bad, src, bad)
        self.assertIsNone(re.search(r"\$\s?[\d{%]", src))      # no dollar amounts (regex anchors are fine)
        self.assertIsNone(re.search(r"ET", src))

    def test_core_section_is_the_shared_code(self):
        """The pasted india_core part must equal the source of truth, byte for byte."""
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        core = read(os.path.join(root, "_shared", "india_core.py")).split("\n")
        end = core.index("    return inr(x, 0)") + 1          # console .. money: what a scanner pastes
        part = "\n".join(core[11:end])
        self.assertIn(part, read(os.path.join(root, "nse-scanner", "scan.py")))


if __name__ == "__main__":
    unittest.main()
