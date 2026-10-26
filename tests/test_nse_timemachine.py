import copy
import datetime
import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import types
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("nse-timemachine", "timemachine")

DIR = os.path.join(FIX, "nse-timemachine")
REL = os.path.join(DIR, "RELIANCE-60d5m.json.gz")   # Yahoo v8 chart RELIANCE.NS, range=60d interval=5m, fetched 2026-10-26 10:28 IST
with gzip.open(REL, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = "2026-10-26 10:30"
NOW_UTC = tm.parse_now(NOW)

# a plan as nse-exit writes it: the US plan shape plus product, exchange, yahoo, flat_by
PLAN = {"created_by": "nse-exit", "symbol": "RELIANCE", "exchange": "NSE", "yahoo": "RELIANCE.NS", "product": "MIS",
        "side": "buy", "date": "2026-10-26", "setup": "orb",
        "entry": 1193.60, "entry_time": "10:00", "entry_bar": 8, "interval": "5m",
        "opening_range": {"high": 1192.0, "low": 1175.3, "minutes": 15},
        "stop": 1183.65, "stop_rule": "or_mid", "target": 1203.55, "target_r": 1.0,
        "risk_per_share": 9.95, "reward_risk": 1.0, "flat_by": "15:10"}

# fingerprints of the seed-19 run on the fixture: a change in the draws, the pool or the order shows up here
BANDS = "a1b863667c7c5d09"
PATHS = "408326585652338e"
TRADES = "a0a12e5f5ff1821d"
HIST = {"lo": 1172.0, "hi": 1214.5,
        "bins": [96, 90, 132, 211, 301, 369, 483, 602, 639, 712, 741, 769, 763, 733, 703, 617, 501, 386, 333, 229,
                 174, 126, 77, 53]}


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def ist(ts):
    return tm.to_ist(tm.utc_of(ts))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


def full_dates():
    bars = tm.parse_bars(tm.chart_result(TEXT, "RELIANCE"), NOW_UTC)[1]
    return [d for d, _ in tm.full_sessions(tm.split_sessions(bars), "2026-10-26")[0]]


class Reel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"], m["exit_rule"]),
                         (10000, 19, 2, "15:10", "flat_by"))
        self.assertEqual(m["pool_sessions"], 58)
        self.assertEqual(m["bars_left"], 62)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-07-30", "2026-10-23"))
        self.assertEqual(m["p_target"], 0.2326)
        self.assertEqual(m["p_stop"], 0.3049)
        self.assertEqual(m["p_neither"], 0.4625)
        self.assertEqual(m["p_green"], 0.4244)
        self.assertEqual((m["median_close"], m["p5_close"], m["p95_close"]), (1192.8, 1178.41, 1207.71))
        self.assertEqual(m["avg_R"], -0.162)
        self.assertEqual((m["r_at_target"], m["gross_r"]), (0.8186, 1.0))

    def test_fingerprints_of_the_draws(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), PATHS)
        self.assertEqual(m["hist"], HIST)
        self.assertEqual(m["bands"][-1], [1179.01, 1186.69, 1192.86, 1199.15, 1208.03])

    def test_charges_are_in_r(self):
        c = self.doc["costs"]
        self.assertEqual((c["qty"], c["qty_basis"]), (83, "reference"))      # about Rs 1,00,000 at 1193.60
        # recompute 1R from the shared charges function, independently of the Econ class
        at_stop = tm.trade_charges(83 * 1193.60, 83 * 1183.65, "MIS", "NSE")["total"]
        at_target = tm.trade_charges(83 * 1193.60, 83 * 1203.55, "MIS", "NSE")["total"]
        self.assertEqual(c["charges_at_stop"], round(at_stop, 2))
        self.assertEqual(c["one_R"], round(83 * 9.95 + at_stop, 2))          # max loss = risk + charges at the stop
        self.assertEqual(c["net_at_target"], round(83 * 9.95 - at_target, 2))
        self.assertEqual(c["r_at_target"], round((83 * 9.95 - at_target) / (83 * 9.95 + at_stop), 4))
        self.assertLess(c["r_at_target"], 1.0)                               # a 1R target nets less than 1R
        self.assertEqual((c["charges_at_stop"], c["one_R"], c["net_at_target"]), (82.12, 907.97, 743.26))

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (58, 26))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (13, 10, 3, 13))
        self.assertEqual(h["avg_R"], -0.127)
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0})
        slim = [{"date": t["date"], "entry": t["entry"], "outcome": t["outcome"], "R": t["R"]} for t in h["trades"]]
        self.assertEqual(sha(slim), TRADES)
        first = h["trades"][0]
        self.assertEqual((first["date"], first["time"], first["entry"], first["stop"], first["target"],
                          first["outcome"]), ("2026-07-30", "10:40", 1283.85, 1279.3, 1288.4, "target"))
        for t in h["trades"]:       # every price sits on the 0.05 tick
            for k in ("entry", "stop", "target"):
                self.assertAlmostEqual(t[k] / 0.05, round(t[k] / 0.05), places=6, msg=t)
        # a small range makes the fixed charges a large share of the risk: the winner nets well under 1R
        self.assertEqual(first["R"], 0.63)

    def test_the_verdict(self):
        self.assertEqual(self.doc["verdict"],
                         "Leans toward the stop: after charges the 1R target is worth 0.82R, so it has to come "
                         "first in 55 % of the replays that hit one side; it did in 43 % (target first 23.3 % "
                         "vs stop first 30.5 %).")
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), round(even, 3)), (0.433, 0.550))
        self.assertEqual(self.doc["verdict_notes"],
                         ["46.2 % of the replays hit neither by 15:10; the price at 15:10 decides those."])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1193.6] * 5)
        self.assertEqual(len(m["bands"]), 63)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("10:00", "15:10", 63))
        self.assertTrue(all(p[0] == 1193.6 and len(p) == 63 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["yahoo"], p["exchange"], p["product"]), ("RELIANCE", "RELIANCE.NS", "NSE", "MIS"))
        self.assertEqual((p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         (1193.6, 1183.65, 1203.55, 8, "10:00"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"]), (9.95, 1.0, "15:10"))
        self.assertEqual(self.doc["warnings"], [])
        self.assertEqual(self.doc["created_by"], "nse-timemachine")

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("RELIANCE buy (NSE, MIS), Monday 2026-10-26: entry ₹1,193.60 at 10:00 (bar 8), "
                     "stop ₹1,183.65, target ₹1,203.55 (1R, risk ₹9.95 a share)",
                     "10,000 replays", "62 five-minute bars", "the plan's flat-by exit", "seed 19", "58 full sessions",
                     "target first   23.3 %", "stop first     30.5 %",
                     "neither        46.2 %   still open at 15:10 (squared off there)",
                     "green          42.4 %", "median ₹1,192.80, 90 % of replays between ₹1,178.41 and ₹1,207.71",
                     "Average R: -0.162 (net of charges)",
                     "1R = ₹907.97 = ₹825.85 risk + ₹82.12 charges if the stop is hit",
                     "nets ₹743.26 after ₹82.59 charges = +0.82R (a 1R target before charges)",
                     "fired 26 times: 13 target, 10 stop, 3 neither. Average R -0.127",
                     "Left out 1 short session(s)", "2026-10-15", "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("$", "profit and loss", "guarantee", "real time", "tick by tick", "New York", "NYSE"):
            self.assertNotIn(word, text)
        self.assertNotIn("CNC (delivery)", text)


class RealExitPlan(unittest.TestCase):
    """nse/plan.json exactly as nse-exit/exit.py plan --write produced it (1R target, charges for 100 shares)."""

    def test_the_plan_from_exit_py_runs(self):
        path = os.path.join(DIR, "plan-from-exit.json")
        code, text, err = run(["run", "--plan", path, "--offline", REL, "--now", NOW, "--replays", "500", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        p, c = doc["plan"], doc["costs"]
        self.assertEqual((p["symbol"], p["product"], p["entry_time"], p["entry_bar"], p["flat_by"]),
                         ("RELIANCE", "MIS", "10:00", 8, "15:10"))
        self.assertEqual((p["entry"], p["stop"], p["target"]), (1193.6, 1183.65, 1203.55))
        self.assertEqual((c["qty"], c["qty_basis"]), (100, "plan_charges"))
        at_stop = tm.trade_charges(100 * 1193.60, 100 * 1183.65, "MIS", "NSE")["total"]
        self.assertEqual(c["charges_at_stop"], round(at_stop, 2))
        self.assertEqual(c["charges_at_stop"], 89.27)          # the same number exit.py printed
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_mid")
        out = chr(10).join(tm.report(machine(tm.normalize_plan(json.load(open(path, encoding="utf-8"))), replays=200)))
        self.assertIn("(100 shares, reference position from the plan)", out)

    def test_an_explicit_quantity_beats_the_plans_charges(self):
        raw = dict(PLAN, charges={"qty": 100})
        self.assertEqual(tm.normalize_plan(raw)["quantity"], 100)
        self.assertEqual(tm.normalize_plan(dict(raw, quantity=7))["quantity"], 7)
        self.assertEqual(machine(tm.normalize_plan(dict(raw, quantity=7)), replays=50)["costs"]["qty_basis"], "plan")
        self.assertIsNone(tm.normalize_plan(dict(PLAN, charges="n/a"))["quantity"])


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertNotEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.2326, 0.3049, 1192.8))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), BANDS)

    def test_the_draws_are_stable_across_pythons(self):
        # random.Random(seed), randint and choice are stable since Python 3.2; the fingerprints above
        # are the real proof, this pins the first draws so a failure points at the RNG
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(58)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "RELIANCE")

    def test_the_in_progress_tick_is_dropped(self):
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        # 113 null bars: Yahoo's NSE data never has the 15:20 and 15:25 bars (57 + 48 + 6 at 15:15, 2 stray)
        self.assertEqual(dropped, {"no_price": 113, "off_grid": 1, "not_complete": 1, "outside_hours": 0})
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-10-26", "10:20", 13))
        # with the real clock (any time later) the data's own time (10:28:14) still marks the 10:25 bar as in progress
        self.assertEqual(tm.parse_bars(self.result, tm.parse_now(None))[1][-1]["start"], "10:20")
        entry = [b for b in bars if b["date"] == "2026-10-26" and b["slot"] == 8][0]
        self.assertEqual((entry["start"], entry["c"]), ("09:55", 1193.5999755859375))
        self.assertEqual(meta["gmtoffset"], 19800)

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, tm.parse_now("2026-10-26 09:58"))[1]
        today = [b for b in bars if b["date"] == "2026-10-26"]
        self.assertEqual(len(today), 8)
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-10-26 09:58"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("09:50 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-10-26 10:00"), replays=200)["mc"]["pool_sessions"], 58)

    def test_ist_has_no_daylight_saving(self):
        stamps = [1793367000 + 15 * 3600,   # chosen so each lands on a known IST wall-clock time
                  1793367000 + 15 * 3600 + 300]
        res = {"meta": {"symbol": "X.NS"}, "timestamp": stamps,
               "indicators": {"quote": [{"open": [1] * 2, "high": [1] * 2, "low": [1] * 2, "close": [1] * 2,
                                         "volume": [0] * 2}]}}
        meta, bars, dropped = tm.parse_bars(res, tm.parse_now("2027-01-01T00:00Z"))
        for b, ts in zip(bars, stamps):
            ist_time = tm.utc_of(ts) + datetime.timedelta(hours=5, minutes=30)
            self.assertEqual((b["date"], b["start"]), (ist_time.date().isoformat(), ist_time.strftime("%H:%M")))
        # 03:45Z is 09:15 IST in every month of the year
        for month in (1, 7):
            utc = datetime.datetime(2026, month, 5, 3, 45)
            res["timestamp"] = [tm.ts_of(utc)]
            for k in res["indicators"]["quote"][0]:
                res["indicators"]["quote"][0][k] = [1]
            self.assertEqual(tm.parse_bars(res, tm.parse_now("2027-01-01T00:00Z"))[1][0]["slot"], 0)

    def test_pre_open_and_closing_auction_are_dropped(self):
        d = datetime.datetime(2026, 10, 23)
        stamps = [tm.ts_of(tm.ist_to_utc(d.replace(hour=h, minute=m))) for h, m in ((9, 0), (9, 15), (15, 25), (15, 30), (16, 0))]
        res = {"meta": {"symbol": "X.NS"}, "timestamp": stamps,
               "indicators": {"quote": [{k: [1] * 5 for k in ("open", "high", "low", "close", "volume")}]}}
        meta, bars, dropped = tm.parse_bars(res, tm.parse_now("2026-10-24T00:00Z"))
        self.assertEqual([(b["start"], b["slot"]) for b in bars], [("09:15", 0), ("15:25", 74)])
        self.assertEqual(dropped["outside_hours"], 3)

    def test_a_short_session_is_left_out(self):
        def cut(res):  # 2026-09-04 ends at 13:00, like a short special session: 45 bars
            keep_bars(res, lambda ts: not (ist(ts).date().isoformat() == "2026-09-04" and ist(ts).hour >= 13))
        doc = machine(text=edited(cut), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 57)
        self.assertIn({"date": "2026-09-04", "bars": 45}, doc["data"]["short_sessions_left_out"])
        self.assertEqual(doc["history"]["sessions"], 57)
        self.assertNotIn("2026-09-04", [t["date"] for t in doc["history"]["trades"]])
        self.assertIn("2026-09-04", "\n".join(tm.report(doc)))

    def test_the_missing_closing_bars_do_not_cost_sessions(self):
        # 15:20 and 15:25 are null on every day in the fixture and the usable session ends at 15:15
        self.assertEqual(tm.SESSION_BARS, 72)
        self.assertEqual(len(full_dates()), 58)
        self.assertEqual(tm.CLOSE_MIN, 15 * 60 + 15)

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-10-23")
        self.assertNotIn("2026-10-26", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_price_is_dropped(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 114)
        self.assertEqual(doc["mc"]["pool_sessions"], 57)   # 2026-07-30 now has a gap

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        dates = full_dates()

        def trim(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= dates[-19])
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(trim))
        self.assertIn("only 19 full session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))

        def twenty(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= dates[-20])
        self.assertEqual(machine(text=edited(twenty), replays=200)["mc"]["pool_sessions"], 20)

    def test_the_data_must_be_the_plans_ticker(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="TCS", yahoo=None))
        self.assertIn("data is for RELIANCE.NS but the plan is for TCS.NS", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # the BSE listing is a different instrument
            machine(plan(symbol="RELIANCE.BO", yahoo=None))
        self.assertIn("RELIANCE.BO", str(cm.exception))


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-27"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("no bars for that day", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # 10:30 has not happened in this data yet
            machine(plan(entry_time="10:40", entry_bar=None))
        self.assertIn("not in the data", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="10:02", entry_bar=None), "5-minute grid"),
            (dict(entry_time="15:30", entry_bar=None), "between 09:20 and 15:10"),
            (dict(entry_bar=9), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=1195.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol="", yahoo=None), "ticker"),
            (dict(symbol="NIFTY", yahoo=None), "index"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="Oct 26"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
            (dict(product="NRML"), "MIS (intraday) or CNC"),
            (dict(product="CNC", side="sell", stop=1203.55, target=1183.65), "short sale"),
            (dict(flat_by="soon"), "flat_by"),
            (dict(quantity=0), "at least 1"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_closed_days_are_refused_with_the_reason(self):
        for date, words in (("2026-10-20", "Dussehra"), ("2026-10-24", "weekend"), ("2026-11-10", "Diwali")):
            with self.assertRaises(tm.TMError, msg=date) as cm:
                plan(date=date)
            self.assertIn("not an NSE trading day", str(cm.exception))
            self.assertIn(words, str(cm.exception))

    def test_a_special_session_is_refused(self):
        saved = tm.SPECIAL_SESSIONS["2026-11-08"]
        tm.SPECIAL_SESSIONS["2026-11-08"] = {"open": datetime.time(18, 0), "close": datetime.time(19, 0),
                                             "name": "Muhurat trading"}
        try:
            with self.assertRaises(tm.TMError) as cm:
                plan(date="2026-11-08")
            self.assertIn("special session (Muhurat trading)", str(cm.exception))
        finally:
            tm.SPECIAL_SESSIONS["2026-11-08"] = saved

    def test_the_built_in_muhurat_session_is_refused_with_its_real_name(self):
        with self.assertRaises(tm.TMError) as cm:
            plan(date="2026-11-08")
        self.assertIn("special session (Diwali Muhurat trading)", str(cm.exception))

    def test_new_plan_fields_have_defaults(self):
        raw = {k: v for k, v in PLAN.items() if k not in ("product", "exchange", "yahoo", "flat_by")}
        p = tm.normalize_plan(raw)
        self.assertEqual((p["product"], p["exchange"], p["yahoo"], p["flat_by"]),
                         ("MIS", "NSE", "RELIANCE.NS", "15:10"))
        self.assertEqual(tm.normalize_plan(raw, cfg={"flat_by": "15:00"})["flat_by"], "15:00")
        bse = tm.normalize_plan(dict(raw, symbol="RELIANCE.BO"))
        self.assertEqual((bse["exchange"], bse["yahoo"], bse["symbol"]), ("BSE", "RELIANCE.BO", "RELIANCE"))
        self.assertEqual(tm.normalize_plan(dict(raw, exchange="BSE"))["yahoo"], "RELIANCE.BO")
        self.assertEqual(tm.normalize_plan(dict(raw, symbol="M&M"))["yahoo"], "M&M.NS")

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "10:00")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 8)
        self.assertEqual(plan(entry_time="10:00 AM", entry_bar=None)["entry_bar"], 8)
        self.assertEqual(plan(entry_time="10:00am", entry_bar=None)["entry_bar"], 8)
        self.assertEqual(plan(entry_time="3:05 PM", entry_bar=None)["entry_bar"], 69)

    def test_flat_by_sets_the_exit_for_mis(self):
        doc = machine(plan(flat_by="15:00"), replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1], m["exit_rule"]), ("15:00", 60, "15:00", "flat_by"))
        self.assertIn("still open at 15:00 (squared off there)", "\n".join(tm.report(doc)))
        # a flat_by between bars rounds down to the bar before
        self.assertEqual(machine(plan(flat_by="15:12"), replays=100)["mc"]["until"], "15:10")
        # entry at or after flat_by leaves nothing to replay
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(flat_by="10:00"), replays=10)
        self.assertIn("nothing to replay", str(cm.exception))

    def test_a_late_flat_by_warns_and_stops_at_the_last_bar(self):
        doc = machine(plan(flat_by="15:25"), replays=100)
        self.assertEqual(doc["mc"]["until"], "15:15")
        self.assertIn("automatic MIS square-off", doc["warnings"][0])

    def test_until_cuts_the_replay(self):
        doc = machine(until="15:05", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1], m["exit_rule"]), ("15:05", 61, "15:05", "until"))
        self.assertEqual(machine(until="3:05 PM", replays=500)["mc"], m)
        self.assertEqual(machine(until="15:15", replays=100)["warnings"],
                         ["--until 15:15 is later than the plan's flat_by 15:10: a MIS trade should be out by then"])
        for bad, words in (("10:00", "nothing to replay"), ("15:20", "no later than 15:15"),
                           ("15:07", "5-minute grid"), ("noon", "India time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_cnc_says_it_is_only_the_intraday_replay(self):
        doc = machine(plan(product="CNC"), replays=2000)
        m, c = doc["mc"], doc["costs"]
        self.assertEqual((m["until"], m["exit_rule"]), ("15:15", "close"))
        self.assertIn("does not model overnight holding", doc["product_note"])
        self.assertIn("T+1", doc["product_note"])
        self.assertIn("does not model overnight holding", "\n".join(tm.report(doc)))
        self.assertNotIn("squared off", "\n".join(tm.report(doc)))
        # delivery charges: STT on both sides, no brokerage, DP charge on the sale
        at_stop = tm.trade_charges(83 * 1193.60, 83 * 1183.65, "CNC", "NSE")
        self.assertEqual(c["charges_at_stop"], at_stop["total"])
        self.assertEqual(at_stop["dp_charge"], 15.34)
        self.assertGreater(c["charges_at_stop"], 200)
        self.assertLess(c["r_at_target"], machine(replays=10)["costs"]["r_at_target"])
        self.assertNotIn("product_note", machine(replays=10))

    def test_an_explicit_quantity_changes_the_charges_not_the_shape(self):
        small = machine(plan(quantity=10), replays=200)
        self.assertEqual((small["costs"]["qty"], small["costs"]["qty_basis"]), (10, "plan"))
        self.assertIn("your plan's quantity", "\n".join(tm.report(small)))
        self.assertLess(small["costs"]["r_at_target"], machine(replays=10)["costs"]["r_at_target"])
        self.assertEqual(machine(replays=10, qty=10)["costs"]["qty"], 10)

    def test_config_charges_overrides_flow_through(self):
        free = {"charges": {"brokerage_pct_mis": 0, "stt_pct_mis_sell": 0, "txn_pct_nse": 0, "sebi_per_crore": 0,
                            "stamp_pct_mis_buy": 0}}
        doc = machine(replays=200, cfg=free)
        self.assertEqual(doc["costs"]["charges_at_stop"], 0.0)
        self.assertEqual(doc["mc"]["r_at_target"], 1.0)      # no charges: a 1R target is +1R
        self.assertNotIn("after charges", doc["verdict"])

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1210.0, stop=1200.0, target=1220.0), replays=200)
        self.assertIn("outside that bar's range", doc["warnings"][0])

    def test_stops_round_away_from_the_entry_to_the_tick(self):
        self.assertEqual(tm.tick_away(1183.68, True), 1183.65)
        self.assertEqual(tm.tick_away(1183.68, False), 1183.70)
        self.assertEqual(tm.tick_away(1175.300048828125, True), 1175.3)
        self.assertEqual(tm.tick_away(1175.29999999, True), 1175.3)

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")           # 1183.65 is the tick-rounded midpoint of 1192.00 / 1175.30
        rule = machine(plan(stop_rule=None, stop=1175.3, target=1211.9), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=1180.0, target=1220.0), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"]), ("pct", 1.1394))
        self.assertEqual(rule["target_r"], 1.94)
        self.assertEqual(plan(stop_rule="or-mid")["stop_rule"], "or_mid")

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 58)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=1203.55, target=1183.65, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertLess(doc["costs"]["r_at_target"], 1.0)
        self.assertEqual(doc["plan"]["side"], "sell")
        self.assertIn("finished in profit after charges", "\n".join(tm.report(doc)))

    def test_the_verdict_thresholds_at_1r_without_charges(self):
        # a 1R target worth exactly 1R (no gross_r key): the target's share against 50 %
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:05"}),
                         "No replay reached the target or the stop by 15:05: the price at 15:05 decides every one.")

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Strongly toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_charges_move_the_break_even(self):
        # gross 2R that nets 1.73R after charges needs 37 % of the decided replays, not 33 %
        doc = machine(plan(target=1213.5))
        m = doc["mc"]
        self.assertEqual((m["gross_r"], m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (2.0, 1.7279, 0.0237, 0.3057, 0.6706, -0.163))
        self.assertEqual(doc["verdict"], "Strongly toward the stop: after charges the 2R target is worth 1.73R, so it "
                                         "has to come first in 37 % of the replays that hit one side; it did in 7 % "
                                         "(target first 2.4 % vs stop first 30.6 %).")
        self.assertEqual(doc["verdict_notes"],
                         ["67.1 % of the replays hit neither by 15:10; the price at 15:10 decides those."])

    def test_a_narrow_target_is_close_to_break_even(self):
        # a small gross R with a wide stop: charges eat most of the target's worth
        doc = machine(plan(stop=1181.0, target=1199.6, stop_rule="pct"))
        m = doc["mc"]
        self.assertEqual((m["gross_r"], m["r_at_target"], m["p_target"], m["p_stop"]), (0.4762, 0.3684, 0.4551, 0.1756))
        self.assertTrue(doc["verdict"].startswith("Close to break-even: after charges the 0.48R target is worth 0.37R"))


class ClosingAuction(unittest.TestCase):
    """v1.1: the forced exit is min(flat_by, continuous_close); the square-off is the broker's."""

    def test_cas_vs_non_cas_stock_on_2026_10_26(self):
        cas = machine(plan(cas_stock=True, flat_by="15:07"), replays=50)
        plain = machine(plan(cas_stock=False, flat_by="15:07"), replays=50)
        self.assertEqual((cas["mc"]["squareoff"], plain["mc"]["squareoff"]), ("15:12", "15:25"))
        self.assertTrue(any("Closing auction" in w and "15:15, not 15:30" in w for w in cas["warnings"]))
        self.assertFalse(any("Closing auction" in w for w in plain["warnings"]))
        self.assertEqual((cas["mc"]["until"], plain["mc"]["until"]), ("15:05", "15:05"))
        # a flat_by after the end of continuous trading is cut to it: 15:15, not 15:20
        late = machine(plan(cas_stock=True, flat_by="15:20"), replays=50)
        self.assertEqual((late["mc"]["until"], late["mc"]["exit_rule"]), ("15:15", "flat_by"))
        self.assertTrue(any("at or after" in w and "15:12" in w for w in late["warnings"]))
        # --until cannot pass the end of continuous trading
        with self.assertRaises(tm.TMError):
            machine(plan(cas_stock=True), until="15:20", replays=50)

    def test_broker_specific_square_off_in_the_warnings(self):
        for broker, so in (("zerodha", "15:12"), ("dhan", "15:10")):
            out = machine(plan(cas_stock=True, flat_by="15:10"), cfg={"broker": broker}, replays=50)
            self.assertEqual(out["mc"]["squareoff"], so, broker)
        out = machine(plan(cas_stock=True, flat_by="15:10"), cfg={"broker": "dhan"}, replays=50)
        self.assertTrue(any("Dhan's automatic MIS square-off (15:10" in w for w in out["warnings"]))
        out = machine(plan(cas_stock=True, flat_by="15:00"), cfg={"broker": "sbi"}, replays=50)
        self.assertTrue(any("not verified" in w for w in out["warnings"]))
        # delivery has no square-off
        out = machine(plan(cas_stock=True, product="CNC"), replays=50)
        self.assertIsNone(out["mc"]["squareoff"])

    def test_unknown_f_and_o_list_is_noted_in_the_plan_warnings(self):
        out = machine(plan(cas_stock=True, fo_note="the F&O list could not be fetched: treating the stock as F&O"),
                      replays=50)
        self.assertIn("treating the stock as F&O", out["warnings"][0])

    def test_the_2026_12_07_switch_back_to_1530(self):
        shift = 42 * 86400            # 2026-10-26 + 42 days = Monday 2026-12-07

        def move(r):
            r["timestamp"] = [x + shift for x in r["timestamp"]]
            r["meta"]["regularMarketTime"] += shift
        text = edited(move)
        now = tm.parse_now("2026-12-07 10:30")
        out = tm.time_machine(text, plan(date="2026-12-07", cas_stock=True, flat_by="15:10"), now, replays=50)
        self.assertFalse(any("Closing auction" in w for w in out["warnings"]))     # no auction shortens the day
        self.assertEqual(out["mc"]["squareoff"], "15:25")
        self.assertTrue(any("2026-12-07" in w or "before SEBI" in w for w in out["warnings"]))
        self.assertEqual(out["mc"]["until"], "15:10")
        before = shift - 3 * 86400    # Friday 2026-12-04, last day of the 15:15 rule
        self.assertEqual(tm.continuous_close(datetime.date(2026, 12, 4), True), datetime.time(15, 15))
        self.assertEqual(tm.continuous_close(datetime.date(2026, 12, 7), True), datetime.time(15, 30))

    def test_the_cli_looks_the_stock_up_in_the_f_and_o_list(self):
        d = tempfile.mkdtemp()
        try:
            for sym, cas in (("RELIANCE", True), ("AWL", False)):
                pl = os.path.join(d, sym + ".json")
                with open(pl, "w") as fh:
                    json.dump(dict(PLAN, symbol=sym, yahoo=sym + ".NS"), fh)
                text = TEXT.replace("RELIANCE.NS", sym + ".NS")
                chart = os.path.join(d, sym + "-chart.json")
                with open(chart, "w", encoding="utf-8") as fh:
                    fh.write(text)
                code, out, err = run(["run", "--plan", pl, "--offline", chart, "--now", NOW, "--json", "--replays", "50"])
                self.assertEqual(code, 0, err)
                doc = json.loads(out)
                self.assertEqual(doc["plan"]["cas_stock"], cas, sym)
        finally:
            shutil.rmtree(d)


class FakeNet(object):
    """Stands in for urllib: answers each request with the next status code."""

    def __init__(self, codes):
        self.codes, self.calls, self.sleeps = list(codes), [], []

    def open_url(self, url, timeout=20, accept="application/json"):
        self.calls.append(url)
        code = self.codes.pop(0)
        if code != 200:
            body = (b'{"chart":{"result":null,"error":{"code":"Not Found","description":"No data found, '
                    b'symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(body))
        return TEXT


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (tm._open_url, tm.time)

    def tearDown(self):
        tm._open_url, tm.time = self.saved

    def net(self, codes):
        f = FakeNet(codes)
        tm._open_url = f.open_url
        tm.time = types.SimpleNamespace(sleep=f.sleeps.append, time=self.saved[1].time)
        return f

    def hosts(self, f):
        return [u.split("//")[1].split(".")[0] for u in f.calls]

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_path("RELIANCE.NS", "60d", "5m"),
                         "/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", tm.chart_path("M&M.NS", "60d", "5m"))     # & must be escaped
        self.assertEqual(tm.UA, "Mozilla/5.0 (compatible; dalal-street-skills/1.0)")
        f = self.net([200])
        self.assertEqual(tm.fetch_chart("RELIANCE.NS"), TEXT)
        self.assertEqual(f.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m"])

    def test_429_waits_retries_then_falls_back_to_query1(self):
        f = self.net([429, 429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE.NS"), TEXT)
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        self.assertEqual(f.sleeps, [tm.BACKOFF])

    def test_one_429_then_ok(self):
        f = self.net([429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE.NS"), TEXT)
        self.assertEqual(self.hosts(f), ["query2", "query2"])

    def test_5xx_falls_back(self):
        f = self.net([503, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE.NS"), TEXT)
        self.assertEqual(self.hosts(f), ["query2", "query1"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.net([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("RELIANCE.NS")
        self.assertIn("Yahoo did not answer", str(cm.exception))
        self.assertIn("HTTP 429", str(cm.exception))

    def test_404_is_yahoos_own_answer(self):
        self.net([404])
        text = tm.fetch_chart("ZZZZ.NS")
        with self.assertRaises(tm.TMError) as cm:
            tm.chart_result(text, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))

    def test_run_over_the_network_path(self):
        f = self.net([200])
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(PLAN, fh)
            fo = os.path.join(FIX, "_shared", "fo_mktlots.csv")
            code, text, _ = run(["run", "--plan", p, "--now", NOW, "--replays", "300", "--fo-lots", fo])
            self.assertEqual(code, 0)
            self.assertEqual(self.hosts(f), ["query2"])
            self.assertIn("RELIANCE.NS", f.calls[0])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "nse", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "nse", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", REL, "--now", NOW, "--write", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("target first   23.3 %", text)
        self.assertIn("Leans toward the stop: after charges the 1R target is worth 0.82R", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/nse-budget", text)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history", "costs") if k in doc),
                         ["costs", "history", "mc", "plan"])
        self.assertEqual(doc["created_by"], "nse-timemachine")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"]), (0.2326, -0.162))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), PATHS)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (26, -0.127))
        self.assertEqual(doc["plan"]["from"], self.plan)

    def test_json_output(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", REL, "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.3049)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertEqual(doc["verdict"][:21], "Leans toward the stop")
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "reliance", "--date", "2026-10-26", "--entry", "1193.60",
                               "--stop", "1183.65", "--target", "1203.55", "--entry-time", "10:00",
                               "--offline", REL, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"]), (0.2326, 0.4244))
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (13, 10, -0.127))
        code, _, err = run(["run", "--symbol", "RELIANCE", "--entry", "1193.60", "--offline", REL])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_cnc_flag(self):
        code, text, err = run(["run", "--symbol", "RELIANCE", "--date", "2026-10-26", "--entry", "1193.60",
                               "--stop", "1183.65", "--target", "1203.55", "--entry-time", "10:00",
                               "--product", "CNC", "--replays", "300", "--offline", REL, "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertIn("does not model overnight holding", text)

    def test_config_flat_by_and_charges(self):
        raw = {k: v for k, v in PLAN.items() if k != "flat_by"}
        with open(self.plan, "w") as fh:
            json.dump(raw, fh)
        cfg = os.path.join(self.d, "nse", "config.json")
        with open(cfg, "w") as fh:
            json.dump({"flat_by": "14:55", "charges": {"brokerage_cap_mis": 5}}, fh)
        code, text, err = run(["run", "--plan", self.plan, "--config", cfg, "--offline", REL, "--now", NOW,
                               "--replays", "300", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["until"], doc["plan"]["flat_by"]), ("14:55", "14:55"))
        self.assertLess(doc["costs"]["charges_at_stop"], 82.12)       # a cheaper brokerage cap

    def test_offline_folder_and_default_paths(self):
        shutil.copy(REL, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("nse", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("nse", "timemachine.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", REL, "--now", NOW, "--until", "9:00"],
                     ["run", "--plan", self.plan, "--now", "tomorrow"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(["run", "--plan", self.plan, "--offline", REL, "--now", NOW, "--write", self.out])
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("2,000 replay paths from the 10:00 entry (seed 19)", lines[0])
        self.assertTrue(any(l.startswith("  1203.55 |") and l.endswith("  target") for l in lines))
        self.assertTrue(any(l.startswith("  1183.65 |") and l.endswith("  stop") for l in lines))
        self.assertTrue(any(l.startswith("  1193.60 |*") and l.endswith("  entry") for l in lines))
        self.assertIn("10:00", text)
        self.assertIn("15:10", text)
        self.assertEqual(lines[-2:], [self.doc_verdict(), "46.2 % of the replays hit neither by 15:10; the price "
                                                         "at 15:10 decides those."])
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", REL, "--now", NOW])
        self.assertEqual(again, text)

    def doc_verdict(self):
        return machine()["verdict"]


if __name__ == "__main__":
    unittest.main()
