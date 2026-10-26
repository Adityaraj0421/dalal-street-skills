"""nse-entry tests (offline). Fixtures in tests/fixtures/nse-entry/:

RELIANCE.NS.json         RELIANCE's Mon 2026-10-26 five-minute bars, fetched live from Yahoo
TCS.NS.json              (range=1d&interval=5m) at 10:27 IST. They end with the in-progress 10:25 bar
                         and Yahoo's live tick (10:27:20 / 10:27:18).
RELIANCE.NS-1m.json      the same morning in one-minute bars (real Yahoo data).
DEMO.NS-1m-synthetic.json  SYNTHETIC one-minute bars (not market data) for Mon 2026-10-19: a wick above
                         the range high, a close AT the high, a null bar, a live tick.
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, FIX, ROOT

entry = load("nse-entry", "entry")

EDIR = os.path.join(FIX, "nse-entry")
REL = os.path.join(EDIR, "RELIANCE.NS.json")
TCS = os.path.join(EDIR, "TCS.NS.json")
REL1M = os.path.join(EDIR, "RELIANCE.NS-1m.json")
DEMO = os.path.join(EDIR, "DEMO.NS-1m-synthetic.json")
OPEN_TS = 1792986300       # 2026-10-26 09:15 IST
DT = datetime.datetime
DAY = "2026-10-26"


def ist(text):
    return entry.parse_now(text)


def doc(path=REL):
    with open(path) as fh:
        return json.load(fh)


def chart(d=None, sym="RELIANCE.NS"):
    return entry.parse_chart(json.dumps(d if d is not None else doc()), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ist(text), **kw)


def shift_to(d, day, open_time):
    """The same session moved to another date, its first bar at open_time IST."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    new_open = int((DT.combine(day, open_time) - entry.IST_OFFSET - DT(1970, 1, 1)).total_seconds())
    delta = new_open - OPEN_TS
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d


def no_breakout(d=None):
    """Every bar after the range stays under the 1192.00 high."""
    d = copy.deepcopy(d if d is not None else doc())
    q = d["chart"]["result"][0]["indicators"]["quote"][0]
    for i in range(3, len(q["close"])):
        q["close"][i] = min(q["close"][i], 1191.0)
        q["high"][i] = min(q["high"][i], 1191.5)
        q["open"][i] = min(q["open"][i], 1191.0)
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheReel(unittest.TestCase):
    def test_triggers_at_1000_on_the_0955_bar(self):
        r = at("2026-10-26 10:00")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["symbol"], "RELIANCE.NS")
        self.assertEqual(r["opening_range"]["high"], 1192.0)
        self.assertEqual(r["opening_range"]["low"], 1175.3)
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual(t["time"], "10:00 AM")
        self.assertEqual(t["bar_start"], "09:55")
        self.assertEqual(t["bar_index"], 8)
        self.assertEqual(t["price"], 1193.6)
        self.assertEqual(t["close"], 1193.5999755859375)
        self.assertEqual(t["pct_over"], 0.13)
        self.assertEqual(r["level"], 1192.0)
        self.assertEqual(r["prev_close"], 1190.0)

    def test_tcs_triggers_earlier(self):
        r = entry.evaluate(chart(doc(TCS), "TCS.NS"), ist("2026-10-26 10:00"))
        self.assertEqual(r["opening_range"]["high"], 2525.0)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("9:55 AM", 7, 2533.2))

    def test_at_0958_the_0955_bar_is_still_forming(self):
        r = at("2026-10-26 09:58")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:50")
        self.assertEqual(r["last"]["close"], 1186.4)
        self.assertEqual(r["forming"]["bar_start"], "09:55")
        self.assertIn("₹5.60 under the range high ₹1,192.00", r["reason"])
        self.assertEqual(at("2026-10-26 09:59:59")["status"], "waiting")

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-10-26 09:05")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("opens at 9:15", r["reason"])
        r = at("2026-10-26 09:29")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-10-26 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        c = chart()
        self.assertEqual(c["bars"][-1]["t"], 1792990640)          # 10:27:20 is in the file
        r = entry.evaluate(c, entry.utc_of(c["data_time"]))     # read as of the file's own time
        self.assertEqual(r["now"], "2026-10-26 10:27:20")
        self.assertEqual(r["_bars"][-1]["slot"], 13)               # the 10:20 bar
        self.assertEqual(r["forming"]["bar_start"], "10:25")
        self.assertEqual(r["bars_closed"], 14)
        # 10:30: closed by the clock, but Yahoo's last trade (10:27:20) is before its close
        r = at("2026-10-26 10:30")
        self.assertEqual(r["_bars"][-1]["slot"], 13)
        self.assertIn("Yahoo's last trade is 10:27:20", r["lag_note"])
        # a minute later with no newer trade it counts; the 10:27:20 tick never becomes a bar
        r = at("2026-10-26 10:31")
        self.assertEqual(r["_bars"][-1]["slot"], 14)
        self.assertEqual(r["bars_closed"], 15)
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-10-26 15:35")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "10:00 AM")
        text = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text)
        self.assertNotIn("This is the moment", text)

    def test_describe_is_in_rupees_and_ist(self):
        text = "\n".join(entry.describe(at("2026-10-26 10:00")))
        self.assertIn("IST", text)
        self.assertIn("TRIGGERED 10:00 AM at ₹1,193.60", text)
        self.assertIn("Product MIS", text)
        self.assertIn("/nse-exit", text)
        self.assertNotIn("$", text)
        self.assertIn("Nothing was placed.", text)


class Rules(unittest.TestCase):
    def test_volume_confirmation_moves_the_trigger(self):
        r = at("2026-10-26 10:27", min_volume_x=1.4)
        self.assertEqual(r["opening_range"]["avg_volume"], 235375)
        self.assertEqual(r["status"], "waiting")                  # the 09:55 bar is 1.37x, nothing later reaches 1.4x
        r = at("2026-10-26 10:00", min_volume_x=1.4)
        self.assertIn("1.37x the range's volume, under your 1.40x", r["reason"])
        r = at("2026-10-26 10:27", min_volume_x=1.1)
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["price"]), ("10:00 AM", 8, 1193.6))
        self.assertEqual(t["volume_x"], 1.37)
        r = at("2026-10-26 10:27", min_volume_x=1.15)             # 09:55 passes (1.37x)
        self.assertEqual(r["trigger"]["bar_index"], 8)
        r = entry.evaluate(chart(doc(TCS), "TCS.NS"), ist("2026-10-26 10:27"), min_volume_x=1)
        self.assertEqual(r["status"], "waiting")                  # TCS broke out on 75342 vs the range's 160829 average
        self.assertIsNone(r["trigger"])

    def test_level_setup(self):
        r = at("2026-10-26 10:30", setup="level", level=1197)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("10:15 AM", 11, 1199.5))
        # a level under the open: the first eligible close is the 09:25 bar's, at 9:30 (no_entries_before)
        r = at("2026-10-26 10:30", setup="level", level=1183)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:30 AM", 2))
        with self.assertRaises(entry.EntryError):
            at("2026-10-26 10:30", setup="level")

    def test_no_breakout_waits_then_expires_at_the_cutoff(self):
        d = no_breakout()
        self.assertEqual(at("2026-10-26 10:31", d)["status"], "waiting")
        self.assertEqual(at("2026-10-26 14:44", d)["status"], "waiting")
        r = at("2026-10-26 14:45", d)
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 14:45", r["reason"])
        self.assertEqual(r["no_entries_after"], "14:45")
        self.assertEqual(r["no_entries_before"], "09:30")
        self.assertEqual(at("2026-10-26 15:45", d)["phase"], "after-close")

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        d = no_breakout()
        cfg = {"no_entries_before": "09:30", "no_entries_after": "10:30"}
        r = at("2026-10-26 10:30:10", d, cfg=cfg)                 # the 10:25 bar closed by the clock only
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo's last trade is 10:27:20", r["lag_note"])
        self.assertEqual(at("2026-10-26 10:31:10", d, cfg=cfg)["status"], "expired")

    def test_config_cutoffs(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "09:55"}
        r = at("2026-10-26 10:05", cfg=cfg)
        self.assertEqual(r["status"], "expired")
        cfg = {"no_entries_before": "10:10", "no_entries_after": "14:45"}
        r = at("2026-10-26 10:30", cfg=cfg)
        self.assertEqual(r["trigger"]["time"], "10:10 AM")       # the 10:05 bar, first to close at or after 10:10
        # a cutoff after the close is clamped to 30 minutes before continuous trading ends (v1.1)
        cfg = {"no_entries_before": "09:30", "no_entries_after": "16:30"}
        self.assertEqual(at("2026-10-26 10:00", cfg=cfg)["no_entries_after"], "14:45")
        self.assertEqual(at("2026-10-26 10:00", cfg=cfg, cas_stock=True)["no_entries_after"], "14:30")

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-10-26 10:00", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-26 10:00", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(entry.parse_now("2026-10-26 10:00"), DT(2026, 10, 26, 4, 30))
        self.assertEqual(entry.parse_now("2026-10-26T04:30:00Z"), DT(2026, 10, 26, 4, 30))
        self.assertEqual(entry.parse_now("2026-10-26 10:00 IST"), DT(2026, 10, 26, 4, 30))

    def test_or_minutes_30(self):
        r = at("2026-10-26 10:27", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 1192.0)
        self.assertEqual(r["opening_range"]["low"], 1175.3)
        self.assertEqual(r["trigger"]["time"], "10:00 AM")


class OneMinute(unittest.TestCase):
    def c(self, path=DEMO, sym="DEMO.NS"):
        return entry.parse_chart(json.dumps(doc(path)), sym)

    def test_wick_and_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-10-19 09:50:30")
        self.assertEqual(r["opening_range"], {"high": 100.5, "low": 99.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 11000})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("9:44 AM", "09:43", 28, 100.62))
        # the 09:35 bar wicked to 101.00 and closed 100.30, the 09:37 bar closed AT 100.50: neither triggered
        r = entry.evaluate(c, ist("2026-10-19 09:38"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["last"]["close"], 100.5)
        r = entry.evaluate(c, ist("2026-10-19 09:43:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:43")

    def test_null_bar_tick_and_volume(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=3)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:47 AM", 31))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(25, slots)                               # 09:40 came as nulls
        self.assertEqual(slots[-1], 34)                           # 09:50 still forming, 09:50:30 tick dropped
        self.assertEqual(r["forming"]["bar_start"], "09:50")

    def test_real_one_minute_bars(self):
        c = self.c(REL1M, "RELIANCE.NS")
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["opening_range"]["high"], 1192.0)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("9:58 AM", 42, 1193.1))
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"]), ("10:09 AM", 1196.7))

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "DEMO", "--offline", DEMO])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_no_daylight_saving_anywhere(self):
        for day in (datetime.date(2026, 12, 7), datetime.date(2026, 11, 2), datetime.date(2026, 3, 9),
                    datetime.date(2026, 7, 6)):
            d = shift_to(doc(), day, datetime.time(9, 15))
            r = entry.evaluate(chart(d), ist("%s 10:00" % day))
            self.assertEqual(r["status"], "triggered", day)
            self.assertEqual(r["trigger"]["time"], "10:00 AM", day)
            self.assertEqual(r["trigger"]["bar_index"], 8, day)
            self.assertEqual(entry.evaluate(chart(d), ist("%s 09:58" % day))["status"], "waiting", day)
        # 10:00 India time is 04:30 UTC all year
        self.assertEqual(ist("2026-12-07 10:00"), DT(2026, 12, 7, 4, 30))

    def test_special_session_window_overrides_the_config(self):
        day = datetime.date(2026, 11, 8)                  # Muhurat day (Sunday)
        saved = entry.SPECIAL_SESSIONS[day.isoformat()]
        entry.SPECIAL_SESSIONS[day.isoformat()] = {"open": datetime.time(18, 0), "close": datetime.time(19, 0),
                                                   "name": "Muhurat trading"}
        try:
            d = shift_to(no_breakout(), day, datetime.time(18, 0))
            r = entry.evaluate(chart(d), ist("2026-11-08 18:44"))
            self.assertEqual(r["status"], "waiting")
            self.assertEqual((r["opens"], r["closes"]), ("18:00", "19:00"))
            self.assertEqual((r["no_entries_before"], r["no_entries_after"]), ("18:15", "18:45"))
            r = entry.evaluate(chart(d), ist("2026-11-08 18:45"))
            self.assertEqual(r["status"], "expired")
            self.assertIn("Muhurat trading", r["reason"])
            self.assertIn("Special session today", "\n".join(entry.describe(r)))
            # the reel's breakout bar ends at 18:45 here, exactly the cutoff: still allowed
            r = entry.evaluate(chart(shift_to(doc(), day, datetime.time(18, 0))), ist("2026-11-08 18:56"))
            self.assertEqual((r["status"], r["trigger"]["time"]), ("triggered", "6:45 PM"))
            self.assertEqual(r["no_entries_after"], "18:45")
        finally:
            entry.SPECIAL_SESSIONS[day.isoformat()] = saved

    def test_weekend_holiday_and_empty_data(self):
        r = at("2026-10-24 10:30")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertEqual(r["next_open"], "2026-10-26 09:15")
        self.assertIn("weekend", r["reason"])
        r = at("2026-10-20 10:30")
        self.assertIn("Dussehra", r["reason"])
        r = at("2026-11-10 10:30")
        self.assertIn("Diwali-Balipratipada", r["reason"])
        empty = copy.deepcopy(doc())
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-10-26 10:00", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-10-27 10:00")                                # Yahoo sent yesterday's bars only
        self.assertEqual(r["status"], "no-data")
        r = at("2028-03-01 10:30")
        self.assertFalse(r["calendar_known"])
        self.assertIn("covers 2026", "\n".join(entry.describe(r)))

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ.NS")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "RELIANCE.NS")


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and time.sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open = entry._open_url
        p = mock.patch.object(entry.time, "sleep", lambda s: self.sleeps.append(s))
        p.start()
        self.addCleanup(p.stop)
        with open(REL) as fh:
            self.body = fh.read()

    def tearDown(self):
        entry._open_url = self._open

    def answers(self, *codes):
        seq = list(codes)

        def fake(url, timeout=20, accept=None):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def get(self, text="RELIANCE"):
        return entry.get_chart(entry.parse_symbol(text), "5m", datetime.date(2026, 10, 26),
                               today=datetime.date(2026, 10, 26))

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = self.get()
        self.assertEqual(len(c["bars"]), 16)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        self.get()
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        self.get()
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "RELIANCE", "--now", "2026-10-26 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_ticker_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            self.get("ZZZZ")
        self.assertIn("No data found", str(cm.exception))

    def test_wrong_symbol_in_the_answer(self):
        self.answers(200)
        with self.assertRaises(entry.EntryError) as cm:
            self.get("TCS")
        self.assertIn("holds RELIANCE.NS, not TCS.NS", str(cm.exception))

    def test_bse_and_ampersand_urls(self):
        self.answers(200)
        try:
            self.get("RELIANCE.BO")
        except entry.EntryError:
            pass                                               # the fixture is the NSE chart: only the URL matters
        self.assertIn("/chart/RELIANCE.BO?", self.calls[0])
        self.assertIn("/chart/M%26M.NS?", entry.chart_path("M&M.NS", "1d", "5m"))

    def test_ranges_for_past_sessions(self):
        today = datetime.date(2026, 10, 27)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 26), today, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 9), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 9, 5), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 10, 9), today, "1m")


def live_provider(d, lag_until=None, calls=None):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike to 1195.00 (above the high, then it closes under), plus a live tick.
    lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = 1195.0, max(b["h"], 1195.0)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": 1195.0, "h": 1195.0, "l": 1195.0, "c": 1195.0, "v": 0})
        return {"symbol": "RELIANCE.NS", "meta": {}, "bars": bars, "prev_close": 1190.0, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), calls=calls), "2026-10-26 09:52", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:00 AM")
        self.assertEqual(fired[0][0]["trigger"]["price"], 1193.6)
        # it looked at 9:52 (the 9:50 bar in progress), at 9:55:02 (that bar closed 1186.40, the 9:55 bar
        # spiking to 1195 in progress) and at 10:00:02 (the 9:55 bar closed 1193.60)
        self.assertEqual([entry.to_ist(c).strftime("%H:%M:%S") for c in calls], ["09:52:00", "09:55:02", "10:00:02"])
        text = "\n".join(lines)
        self.assertIn("9:50 bar  closed ₹1,186.40  ₹5.60 under ₹1,192.00", text)
        self.assertIn("9:55 bar  closed ₹1,193.60  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        lag = ist("2026-10-26 10:00:30")
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), lag_until=lag, calls=calls),
                                                   "2026-10-26 09:57", every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times, ["09:57:00", "10:00:02", "10:00:32"])   # 10:00:02: Yahoo still at 09:59:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:00 AM")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc()), "2026-10-26 09:00", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn(" 9:25 bar  closed ₹1,184.50  opening range set: high ₹1,192.00, low ₹1,175.30", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 9)    # 9:15 ... 9:55, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-10-26 09:36"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(doc(), calls=calls), clock.now, clock.sleep, lines.append,
                                     dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["09:36:00", "09:54:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in (" 9:30", " 9:35", " 9:40", " 9:45", " 9:50"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "10:00 AM")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-26 14:00", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(entry.to_ist(clock.now()).strftime("%H:%M"), "14:45")
        until = DT(2026, 10, 26, 11, 0)
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-26 10:30", every=300,
                                                   until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-10-24 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-10-26 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-26 10:00", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 10:00 AM at ₹1,193.60", text)
        self.assertIn("/nse-exit", text)
        with open(os.path.join("nse", "entry.json"), encoding="utf-8") as fh:
            e = json.load(fh)
        want = {"created_by": "nse-entry", "symbol": "RELIANCE", "exchange": "NSE", "yahoo": "RELIANCE.NS",
                "product": "MIS", "setup": "orb", "side": "buy", "date": "2026-10-26",
                "status": "triggered", "time": "10:00 AM", "bar_start": "09:55", "bar_index": 8, "price": 1193.6,
                "level": 1192.0, "interval": "5m", "opening_range": {"high": 1192.0, "low": 1175.3, "minutes": 15},
                "source": "yahoo", "checked_at": "2026-10-26T04:30:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)

    def test_bse_suffix_and_cnc_product_are_carried(self):
        os.mkdir("charts")
        shutil.copy(REL, os.path.join("charts", "RELIANCE.BO.json"))
        d = doc()
        d["chart"]["result"][0]["meta"]["symbol"] = "RELIANCE.BO"
        with open(os.path.join("charts", "RELIANCE.BO.json"), "w") as fh:
            json.dump(d, fh)
        code, text, _ = run(["check", "reliance.bo", "--offline", "charts", "--now", "2026-10-26 10:00",
                             "--product", "cnc", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("Product CNC", text)
        with open(os.path.join("nse", "entry.json")) as fh:
            e = json.load(fh)
        self.assertEqual((e["symbol"], e["exchange"], e["yahoo"], e["product"]),
                         ("RELIANCE", "BSE", "RELIANCE.BO", "CNC"))

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-26 09:58", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-26 09:58", "--write", "e.json"])
        with open("e.json") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", text)

    def test_offline_gz_folder_and_mismatch(self):
        os.mkdir("charts")
        with open(REL, "rb") as src, gzip.open(os.path.join("charts", "RELIANCE.NS.json.gz"), "wb") as dst:
            dst.write(src.read())
        code, text, _ = run(["check", "reliance", "--offline", "charts", "--now", "2026-10-26 10:00"])
        self.assertEqual(code, 0)
        code, _, err = run(["check", "TCS", "--offline", REL])
        self.assertEqual(code, 1)
        self.assertIn("holds RELIANCE.NS, not TCS.NS", err)

    def test_longs_only_indices_products_and_flag_errors(self):
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("v1 plans longs only", err)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--level", "300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--setup", "level"])
        self.assertIn("needs --level", err)
        code, _, err = run(["check", "NIFTY", "--offline", REL])
        self.assertIn("is an index", err)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--product", "NRML"])
        self.assertIn("MIS (intraday) or CNC (delivery)", err)
        code, _, err = run(["check", "RELI ANCE", "--offline", REL])
        self.assertIn("not an NSE/BSE ticker", err)

    def test_config_is_read(self):
        os.mkdir("nse")
        with open(os.path.join("nse", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "09:55"}, fh)
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-26 10:05"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("nse", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "RELIANCE", "--offline", REL])
        self.assertIn("not valid JSON", err)

    def test_holidays_json_adds_a_special_session(self):
        os.mkdir("nse")
        with open(os.path.join("nse", "holidays.json"), "w") as fh:
            json.dump({"special_sessions": {"2026-11-08": {"open": "18:00", "close": "19:00",
                                                           "name": "Muhurat trading"}}}, fh)
        saved = (entry._calendar_loaded, dict(entry.SPECIAL_SESSIONS))
        entry._calendar_loaded = False
        try:
            d = shift_to(no_breakout(), datetime.date(2026, 11, 8), datetime.time(18, 0))
            with open("m.json", "w") as fh:
                json.dump(d, fh)
            code, text, _ = run(["check", "RELIANCE", "--offline", "m.json", "--now", "2026-11-08 18:46"])
            self.assertEqual(code, 1)
            self.assertIn("EXPIRED", text)
            self.assertIn("Muhurat trading", text)
        finally:
            entry._calendar_loaded = saved[0]
            entry.SPECIAL_SESSIONS.clear()
            entry.SPECIAL_SESSIONS.update(saved[1])

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "RELIANCE", "--offline", REL, "--now", "2026-10-26 09:30", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn("9:55 bar  closed ₹1,193.60  TRIGGERED", text)
        with open(os.path.join("nse", "entry.json")) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 8)
        code, text, _ = run(["watch", "RELIANCE", "--offline", REL, "--now", "2026-10-26 09:30", "--until", "09:40",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(REL, os.path.join("charts", "RELIANCE.NS.json"))
        shutil.copy(TCS, os.path.join("charts", "TCS.NS.json"))
        shutil.copy(DEMO, os.path.join("charts", "DEMO.NS.json"))
        os.mkdir("nse")
        with open(os.path.join("nse", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "nse-scanner", "hits": [
                {"symbol": "RELIANCE", "setups": ["orb"]}, {"symbol": "TCS", "yahoo": "TCS.NS"},
                {"symbol": "ZZZZ", "setups": ["orb"]}, {"symbol": "DEMO", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-26 10:00"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertEqual(rows[1].split()[:2], ["RELIANCE", "triggered"])
        self.assertIn("10:00 AM", rows[1])
        self.assertIn("₹1,193.60", rows[1])
        self.assertIn("₹2,533.20", rows[2])
        self.assertIn("ZZZZ", rows[3])
        self.assertIn("error: no offline file", rows[3])
        self.assertIn("DEMO", rows[4])
        self.assertIn("error: DEMO.NS has 1m bars", rows[4])
        self.assertIn("2 of 4 triggered", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-26 09:58", "--json"])
        self.assertEqual(code, 0)                                  # TCS had triggered by then
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /nse-scanner first", err)


class ClosingAuction(unittest.TestCase):
    """v1.1: F&O ("CAS") stocks stop continuous trading at 15:15 from 2026-08-03 to 2026-12-06."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_cas_vs_non_cas_stock_on_2026_10_26(self):
        d = no_breakout()
        cas = entry.evaluate(chart(d), ist("2026-10-26 10:00"), cas_stock=True)
        plain = entry.evaluate(chart(d), ist("2026-10-26 10:00"), cas_stock=False)
        self.assertEqual((cas["closes"], plain["closes"]), ("15:15", "15:30"))
        self.assertEqual((cas["no_entries_after"], plain["no_entries_after"]), ("14:30", "14:45"))
        self.assertIn("continuous trading ends at 15:15, not 15:30", cas["auction"])
        self.assertIn("last new entry is 14:30", cas["auction"])
        self.assertIsNone(plain["auction"])
        self.assertIn("Closing auction", "\n".join(entry.describe(cas)))
        # 15:16: the F&O stock's continuous session is over, the other stock's is not
        self.assertEqual(entry.evaluate(chart(d), ist("2026-10-26 15:16"), cas_stock=True)["phase"], "after-close")
        self.assertEqual(entry.evaluate(chart(d), ist("2026-10-26 15:16"), cas_stock=False)["phase"], "after-cutoff")

    def test_a_late_no_entries_after_is_capped_30_minutes_before_continuous_close(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "15:10"}
        cas = entry.evaluate(chart(), ist("2026-10-26 10:00"), cfg=cfg, cas_stock=True)
        plain = entry.evaluate(chart(), ist("2026-10-26 10:00"), cfg=cfg, cas_stock=False)
        self.assertEqual((cas["no_entries_after"], plain["no_entries_after"]), ("14:30", "14:45"))
        self.assertIn("capped", cas["auction"])
        cfg = {"no_entries_before": "09:30", "no_entries_after": "14:45", "entry_buffer_min": 30}
        self.assertEqual(entry.evaluate(chart(), ist("2026-10-26 10:00"), cfg=cfg, cas_stock=True)["no_entries_after"], "14:45")

    def test_the_2026_12_07_switch_back_to_1530(self):
        for day, closes, auction in ((datetime.date(2026, 12, 4), "15:15", True), (datetime.date(2026, 12, 7), "15:30", False)):
            d = shift_to(no_breakout(), day, datetime.time(9, 15))
            r = entry.evaluate(chart(d), ist("%s 10:00" % day), cas_stock=True)
            self.assertEqual(r["closes"], closes, day)
            self.assertEqual(bool(r["auction"]), auction, day)
            self.assertEqual(r["no_entries_after"], "14:30" if auction else "14:45", day)

    def test_muhurat_2026_11_08(self):
        day = datetime.date(2026, 11, 8)
        d = shift_to(no_breakout(), day, datetime.time(14, 20))
        plain = entry.evaluate(chart(d), ist("2026-11-08 14:50"), cas_stock=False)
        cas = entry.evaluate(chart(d), ist("2026-11-08 14:50"), cas_stock=True)
        self.assertEqual((plain["opens"], plain["closes"], plain["special_session"]),
                         ("14:20", "15:30", "Diwali Muhurat trading"))
        self.assertEqual((plain["no_entries_before"], plain["no_entries_after"]), ("14:35", "15:15"))
        self.assertEqual((cas["closes"], cas["no_entries_after"]), ("15:15", "15:00"))

    def test_cli_reads_the_fo_list_fixture_offline(self):
        os.mkdir("charts")
        for name in ("RELIANCE", "AWL"):
            x = doc()
            x["chart"]["result"][0]["meta"]["symbol"] = name + ".NS"
            with open(os.path.join("charts", name + ".NS.json"), "w") as fh:
                json.dump(x, fh)
        code, text, _ = run(["check", "RELIANCE", "--offline", "charts", "--now", "2026-10-26 10:00", "--json"])
        self.assertIn("continuous trading ends at 15:15", json.loads(text)["auction"])
        code, text, _ = run(["check", "AWL", "--offline", "charts", "--now", "2026-10-26 10:00", "--json"])
        self.assertIsNone(json.loads(text)["auction"])
        # a given list wins: AWL listed as F&O
        with open("lots.csv", "w") as fh:
            fh.write("UNDERLYING,SYMBOL,OCT-26\nAsian Paints,AWL,100\n")
        code, text, _ = run(["check", "AWL", "--offline", "charts", "--now", "2026-10-26 10:00", "--json",
                             "--fo-lots", "lots.csv"])
        self.assertIn("continuous trading ends at 15:15", json.loads(text)["auction"])

    def test_live_list_is_cached_reused_when_stale_and_cautious_when_missing(self):
        with open(entry.FO_FIXTURE, encoding="utf-8") as fh:
            fixture = fh.read()
        day = datetime.date(2026, 10, 26)
        sym = entry.parse_symbol("AWL")
        got = []
        cas, note = entry.cas_status(sym, day, fetch=lambda u: got.append(u) or fixture, today=day)
        self.assertEqual((cas, note, got), (False, None, [entry.FO_LOTS_URL]))
        self.assertTrue(os.path.exists(os.path.join("nse", "state", "fo_mktlots-2026-10-26.csv")))
        # today's cache is read without another download
        cas, _ = entry.cas_status(entry.parse_symbol("RELIANCE"), day, fetch=lambda u: 1 / 0, today=day)
        self.assertTrue(cas)

        def boom(u):
            raise entry.IndiaError("no network")
        later = datetime.date(2026, 10, 30)
        cas, note = entry.cas_status(entry.parse_symbol("RELIANCE"), later, fetch=boom, today=later)
        self.assertTrue(cas)
        self.assertIn("cached on 2026-10-26", note)
        much_later = datetime.date(2026, 11, 20)
        cas, note = entry.cas_status(entry.parse_symbol("AWL"), much_later, fetch=boom, today=much_later)
        self.assertTrue(cas)                       # unknown: the cautious answer
        self.assertIn("no cache", note)
        # after 2026-12-07 nothing is downloaded at all
        cas, note = entry.cas_status(entry.parse_symbol("RELIANCE"), datetime.date(2026, 12, 7), fetch=lambda u: 1 / 0)
        self.assertEqual((cas, note), (False, None))


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertIn("## Safety", text)
        low = " ".join(text.lower().split())
        for must in ("on windows, use `py` in place of `python3`", "bar by bar", "can lag",
                     "day trading loses most people money", "/nse-exit", "long side only", "in the background",
                     "/loop", "nothing was placed", "10:00 am", "₹1,193.60", "bar 8", "9:15", "14:45",
                     "mis", "cnc", "nse/entry.json", "muhurat"):
            self.assertIn(must, low)
        for never in ("—", "tick " + "by tick", "real " + "time", "pro" + "fit", "nyse", "new york", "alpaca"):
            self.assertNotIn(never, low)


class Leftovers(unittest.TestCase):
    def test_no_us_leftovers_in_the_script(self):
        with open(os.path.join(ROOT, "nse-entry", "entry.py"), encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("NYSE", "New York", "Eastern", "eastern", "trader/", "Alpaca", "day-trader-skill", "$%"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
