"""nse-exit tests (offline). The bars for the ATR stop are RELIANCE's real Mon 2026-10-26
five-minute bars in tests/fixtures/nse-exit/RELIANCE.NS.json (the same file nse-entry uses)."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal
from unittest import mock

from _load import load, FIX, ROOT

exitp = load("nse-exit", "exit")
entry = load("nse-entry", "entry")

XDIR = os.path.join(FIX, "nse-exit")
REL = os.path.join(XDIR, "RELIANCE.NS.json")
REL_ENTRY_FIX = os.path.join(FIX, "nse-entry", "RELIANCE.NS.json")
RUPEE = "₹"

# nse/entry.json for the reel, exactly as nse-entry writes it
REEL_ENTRY = {"created_by": "nse-entry", "symbol": "RELIANCE", "exchange": "NSE", "yahoo": "RELIANCE.NS",
              "product": "MIS", "setup": "orb", "side": "buy", "date": "2026-10-26",
              "status": "triggered", "time": "10:00 AM", "bar_start": "09:55", "bar_index": 8, "price": 1193.6,
              "level": 1192.0, "interval": "5m", "opening_range": {"high": 1192.0, "low": 1175.3, "minutes": 15},
              "source": "yahoo", "checked_at": "2026-10-26T04:30:00Z"}
# nse/plan.json for the reel with --target 1.5R, the contract (without the charges, checked apart)
REEL_PLAN = {"created_by": "nse-exit", "symbol": "RELIANCE", "exchange": "NSE", "yahoo": "RELIANCE.NS",
             "product": "MIS", "side": "buy", "date": "2026-10-26", "setup": "orb",
             "entry": 1193.6, "entry_time": "10:00 AM", "entry_bar": 8, "interval": "5m",
             "opening_range": {"high": 1192.0, "low": 1175.3, "minutes": 15},
             "stop": 1183.65, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
             "target": 1208.5, "target_r": 1.5, "target_label": "1.5R (1.5 times the risk)",
             "risk_per_share": 9.95, "reward_risk": 1.5, "flat_by": "15:07",
             "cas_stock": True, "squareoff": "15:12", "broker": "zerodha"}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=1192.0, ol=1175.3, atr=None, n=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n)


class Math(unittest.TestCase):
    def test_the_reel_or_mid_on_the_tick(self):
        p = c(1193.6, "or-mid", "1R")
        self.assertEqual(p["stop"], Decimal("1183.65"))           # (1192.00 + 1175.30) / 2, already on the tick
        self.assertEqual(p["risk"], Decimal("9.95"))
        self.assertEqual(p["target"], Decimal("1203.55"))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertEqual(p["stop_detail"], "(₹1,192.00 + ₹1,175.30) / 2 = ₹1,183.65")

    def test_the_default_2r_and_target_rounds_down(self):
        p = c(1193.6, "or-mid")
        self.assertEqual(p["target"], Decimal("1213.50"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")
        p = c(1193.6, "or-mid", "1.5R")
        self.assertEqual(p["target"], Decimal("1208.50"))         # 1208.525 rounds DOWN to the tick
        self.assertEqual(p["reward_risk"], Decimal("1.50"))
        self.assertEqual(p["target_label"], "1.5R (1.5 times the risk)")

    def test_stop_rounds_down_entry_rounds_up(self):
        p = c(1193.6, "or-mid", "1R", 1191.95, 1175.3)
        self.assertEqual(p["stop"], Decimal("1183.60"))           # 1183.625 down to the 0.05 tick
        self.assertIn("rounded down to the 0.05 tick", p["stop_detail"])
        p = c(1193.62, "or-mid", "1R")
        self.assertEqual(p["entry"], Decimal("1193.65"))          # a long's entry limit rounds up
        p = c(1193.6, "price:1183.69")
        self.assertEqual(p["stop"], Decimal("1183.65"))           # away from the entry
        p = c(1193.6, "price:1183.65")
        self.assertEqual(p["stop"], Decimal("1183.65"))
        self.assertNotIn("rounded", p["stop_detail"])

    def test_raw_yahoo_floats_give_the_same_prices(self):
        p = c(1193.5999755859375, "or-mid", "1R", 1192.0, 1175.300048828125)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("1193.60"), Decimal("1183.65"), Decimal("1203.55")))

    def test_other_stops(self):
        p = c(1193.6, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1175.30"), Decimal("18.30"), Decimal("1230.20")))
        p = c(1193.6, "pct:1")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1181.65"), Decimal("11.95"), Decimal("1217.50")))
        self.assertEqual(p["stop_label"], "1% under the entry")
        p = c(1193.6, "atr:1.5", atr=6.3222, n=9)
        self.assertEqual(p["stop"], Decimal("1184.10"))           # 1193.60 - 9.4833 = 1184.1167
        self.assertEqual(p["stop_label"], "1.5 x ATR(9) under the entry")
        self.assertIn("fewer than 14 so far", p["stop_detail"])

    def test_price_targets_and_labels(self):
        p = c(1193.6, "or-mid", "price:1200")
        self.assertEqual(p["target"], Decimal("1200.00"))
        self.assertEqual(p["reward_risk"], Decimal("0.64"))
        self.assertEqual(p["target_label"], "your price (0.64R)")
        p = c(1193.6, "or-mid", "price:1212.57")
        self.assertEqual(p["target"], Decimal("1212.55"))         # a limit sell rounds down too
        p = c(1193.6, "price:₹1,183.65", "price:Rs 1,200")
        self.assertEqual((p["stop"], p["target"]), (Decimal("1183.65"), Decimal("1200.00")))

    def test_refusals_say_why(self):
        cases = [("price:1193.60", "2R", "at or above the entry"),
                 ("price:1195", "2R", "at or above the entry"),
                 ("or-mid", "price:1193", "at or below the entry"),
                 ("price:1193.05", "2R", "too tight to survive the spread"),
                 ("pct:0.04", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1193.6, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # 0.05% of 1193.60 is 0.597: a Rs 0.60 risk is allowed, Rs 0.55 is not
        self.assertEqual(c(1193.6, "price:1193.00")["risk"], Decimal("0.60"))
        # a cheap stock: the floor is two ticks (Rs 0.10)
        self.assertEqual(c(50, "price:49.90", oh=None, ol=None)["risk"], Decimal("0.10"))
        with self.assertRaises(exitp.PlanError):
            c(50, "price:49.95", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1180.0, "or-mid")                                     # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1193.6, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1193.6, "or-mid", oh=1175.3, ol=1192.0)

    def test_charges_use_the_core_numbers_and_both_outcomes(self):
        p = c(1193.6, "or-mid", "1.5R")
        v = exitp.charges_view(p, 100, "MIS", "NSE", None)
        stop = exitp.trade_charges(119360.0, 118365.0, "MIS", "NSE")
        target = exitp.trade_charges(119360.0, 120850.0, "MIS", "NSE")
        self.assertEqual((v["at_stop"], v["at_target"]), (stop["total"], target["total"]))
        self.assertEqual(stop["brokerage"], 40.0)                  # 0.03% capped at Rs 20 a side
        self.assertAlmostEqual(v["net_reward"], 1490.0 - target["total"], places=2)
        self.assertAlmostEqual(v["net_risk"], 995.0 + stop["total"], places=2)
        self.assertAlmostEqual(v["reward_risk_net"], round(v["net_reward"] / v["net_risk"], 2), places=2)
        self.assertLess(v["reward_risk_net"], float(p["reward_risk"]))
        cnc = exitp.charges_view(p, 100, "CNC", "NSE", None)
        self.assertEqual(cnc["breakdown_at_stop"]["brokerage"], 0.0)
        self.assertGreater(cnc["breakdown_at_stop"]["dp_charge"], 0)
        bse = exitp.charges_view(p, 100, "MIS", "BSE", None)
        self.assertGreater(bse["breakdown_at_target"]["exchange_txn"], v["breakdown_at_target"]["exchange_txn"])
        cfg = {"charges": {"brokerage_cap_mis": 5}}
        self.assertEqual(exitp.charges_view(p, 100, "MIS", "NSE", cfg)["breakdown_at_stop"]["brokerage"], 10.0)

    def test_atr_on_the_session_bars(self):
        sym = {"yahoo": "RELIANCE.NS", "tradingsymbol": "RELIANCE"}
        ch = exitp.load_chart(sym, "5m", None, XDIR)               # a folder: RELIANCE.NS.json
        day = datetime.date(2026, 10, 26)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=8)
        self.assertEqual(len(bars), 9)                              # 9:15 ... the 9:55 entry bar
        # by hand: the true range of each bar, the first one without the overnight gap
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 1192.0 - 1182.5, places=4)
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 9)
        self.assertAlmostEqual(atr, sum(trs) / 9, places=9)
        self.assertAlmostEqual(atr, 6.3222, places=4)
        # later it uses the last 14 bars; the file's in-progress 10:25 bar and tick never count
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(all_bars), 14)
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)
        with self.assertRaises(exitp.PlanError):
            exitp.load_chart({"yahoo": "TCS.NS", "tradingsymbol": "TCS"}, "5m", None, REL)   # file holds RELIANCE


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("nse")
        # the F&O list "download" is the fixture: the tests never touch the network
        with open(exitp.FO_FIXTURE, encoding="utf-8") as fh:
            lots = fh.read()
        self.fetched = []
        self.net = mock.patch.object(exitp, "_open_url", lambda url, *a, **k: self.fetched.append(url) or lots)
        self.net.start()

    def tearDown(self):
        self.net.stop()
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-10-26 10:01"] + list(args))

    def read_plan(self):
        with open(os.path.join("nse", "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, _ = run(entry, ["check", "RELIANCE", "--offline", REL_ENTRY_FIX, "--now", "2026-10-26 10:00", "--write"])
        self.assertEqual(code, 0)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1.5R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        ch = p["charges"]
        want = exitp.charges_view(c(1193.6, "or-mid", "1.5R"), 100, "MIS", "NSE", None)
        self.assertEqual((ch["qty"], ch["at_stop"], ch["at_target"]), (100, want["at_stop"], want["at_target"]))
        self.assertEqual(p["reward_risk_net"], want["reward_risk_net"])
        self.assertIn("reference position", ch["basis"])
        self.assertIn("Stop          %s1,183.65  opening-range midpoint" % RUPEE, text)
        self.assertIn("Target        %s1,208.50  1.5R (1.5 times the risk)" % RUPEE, text)
        self.assertIn("Risk a share  %s9.95 (0.83%% of the entry)" % RUPEE, text)
        self.assertIn("Reward:risk   1.5 before charges, %s after" % exitp.ratio(want["reward_risk_net"]), text)
        self.assertIn("Charges       %s at the stop" % exitp.inr(want["at_stop"]), text)
        self.assertIn("Flat by       15:07 IST", text)
        self.assertIn("Square-off    15:12 IST: Zerodha auto-squares-off open MIS F&O positions", text)
        self.assertIn("(NSE MIS)", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/nse-timemachine", text)
        self.assertNotIn("$", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual(self.read_plan()["target"], 1213.5)
        self.assertEqual(self.read_plan()["target_r"], 2.0)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-26 10:20"])
        self.assertEqual(code, 0)
        self.assertIn("%s1,213.50" % RUPEE, text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-27 10:20"])
        self.assertIn("not today", text)

    def test_manual_flags_json_and_bse(self):
        code, text, _ = self.plan("--symbol", "reliance", "--entry", "1193.60", "--or-high", "1192", "--or-low", "1175.30",
                                  "--time", "10:00", "--entry-bar", "8", "--date", "2026-10-26", "--target", "1.5R",
                                  "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        code, text, _ = self.plan("--symbol", "RELIANCE.BO", "--entry", "1193.60", "--stop", "pct:1", "--json")
        p = json.loads(text)
        self.assertEqual((p["symbol"], p["exchange"], p["yahoo"], p["setup"]), ("RELIANCE", "BSE", "RELIANCE.BO", "level"))
        nse = exitp.charges_view(c(1193.6, "pct:1"), 100, "MIS", "NSE", None)
        self.assertGreater(p["charges"]["at_target"], nse["at_target"])    # BSE's exchange charge is higher
        code, _, err = self.plan("--symbol", "NIFTY", "--entry", "24000", "--stop", "pct:1")
        self.assertIn("is an index", err)

    def test_product_cnc_and_the_charges_basis(self):
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, product="CNC"))
        code, text, err = self.plan("--qty", "10", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        self.assertEqual((p["product"], p["charges"]["qty"], p["charges"]["basis"]), ("CNC", 10, "your --qty"))
        self.assertGreater(p["charges"]["breakdown_at_stop"]["dp_charge"], 0)
        self.assertEqual(p["charges"]["breakdown_at_stop"]["brokerage"], 0.0)
        self.assertIn("(NSE CNC)", text)
        self.assertIn("CNC is delivery", text)
        code, text, _ = self.plan("--product", "mis", "--json")      # the flag wins over the file
        self.assertEqual(json.loads(text)["product"], "MIS")
        code, _, err = self.plan("--product", "NRML")
        self.assertIn("MIS (intraday) or CNC (delivery)", err)
        # sized from the config's max loss
        self.write(os.path.join("nse", "config.json"), {"max_loss_per_trade_inr": 500})
        code, text, _ = self.plan("--json")
        p = json.loads(text)
        self.assertEqual(p["charges"]["qty"], 50)                    # floor(500 / 9.95)
        self.assertIn("max_loss_per_trade_inr", p["charges"]["basis"])

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("nse", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", REL, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1184.1, 9.5, 1212.6))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 9))
        self.assertAlmostEqual(p["atr"], 6.3222, places=4)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", REL, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, status="waiting", time=None, price=None,
                                                          bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("RELIANCE is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "1192.05", "--json")    # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (1192.05, 1183.65, None))

        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, setup="level", level=1190.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("v1 plans longs only", err)
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        code, _, err = self.plan("--stop", "price:1193.50")
        self.assertIn("longs only", err)
        os.remove(os.path.join("nse", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--entry", "1193.6", "--or-high", "1192")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_thin_reward_after_charges_is_called_out(self):
        code, text, _ = self.plan("--symbol", "CHEAP", "--entry", "100", "--stop", "price:99.90", "--target", "1R", "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertLess(p["reward_risk_net"], 0)
        self.assertIn("cannot pay for itself", p["warnings"][0])

    def test_flat_by_config_square_off_special_session_and_late_entries(self):
        self.write(os.path.join("nse", "entry.json"), REEL_ENTRY)
        # RELIANCE is an F&O stock: Zerodha squares it off at 15:12, so 15:13 is refused
        self.write(os.path.join("nse", "config.json"), {"flat_by": "15:13", "min_reward_risk": 1.5})
        code, _, err = self.plan("--target", "1R", "--json")
        self.assertEqual(code, 1)
        self.assertIn("Zerodha will square off open MIS positions", err)
        # a stock outside F&O trades to 15:30 and is squared off at 15:25: 15:15 is fine
        self.write(os.path.join("nse", "config.json"), {"flat_by": "15:15", "min_reward_risk": 1.5})
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, symbol="AWL", yahoo="AWL.NS"))
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual((p["flat_by"], p["cas_stock"], p["squareoff"]), ("15:15", False, "15:25"))
        self.write(os.path.join("nse", "entry.json"), REEL_ENTRY)
        self.assertIn("after charges", p["warnings"][0])
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])

        self.write(os.path.join("nse", "config.json"), {"flat_by": "15:25"})
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("end of continuous trading", err)       # after the 15:15 auction start; the square-off is 15:12
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, product="CNC"))
        self.assertEqual(self.plan()[0], 0)                          # delivery is not squared off
        os.remove(os.path.join("nse", "config.json"))

        self.write(os.path.join("nse", "entry.json"), REEL_ENTRY)
        code, _, err = self.plan("--flat-by", "15:45")
        self.assertIn("after the 3:15 PM end of continuous trading", err)
        self.assertIn("closing auction", err)
        code, _, err = self.plan("--flat-by", "15:30")
        self.assertEqual(code, 1)
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, time="3:12 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)
        self.write(os.path.join("nse", "entry.json"), REEL_ENTRY)
        code, text, _ = self.plan("--flat-by", "14:30")
        self.assertIn("Flat by       14:30 IST", text)

    def test_special_session_flat_by_is_close_minus_five(self):
        day = "2026-11-08"
        saved = exitp.SPECIAL_SESSIONS[day]
        exitp.SPECIAL_SESSIONS[day] = {"open": datetime.time(18, 0), "close": datetime.time(19, 0),
                                       "name": "Muhurat trading"}
        try:
            # a stock outside F&O: the session's own close (19:00) less 5 minutes
            outside = dict(REEL_ENTRY, symbol="AWL", yahoo="AWL.NS", date=day)
            self.write(os.path.join("nse", "entry.json"), dict(outside, time="6:50 PM"))
            code, text, err = run(exitp, ["plan", "--now", "2026-11-08 18:51"])
            self.assertEqual(code, 0, err)
            self.assertIn("Flat by       18:55 IST", text)
            self.assertIn("Muhurat trading", text)
            code, _, err = run(exitp, ["plan", "--now", "2026-11-08 18:51", "--flat-by", "19:05"])
            self.assertIn("after the 7:00 PM close", err)
            self.write(os.path.join("nse", "entry.json"), dict(outside, time="6:57 PM"))
            code, _, err = run(exitp, ["plan", "--now", "2026-11-08 18:58"])
            self.assertIn("no time left to hold it", err)
            # an F&O stock stops 15 minutes earlier (18:45): flat by 18:40
            self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, date=day, time="6:30 PM"))
            code, text, err = run(exitp, ["plan", "--now", "2026-11-08 18:31"])
            self.assertEqual(code, 0, err)
            self.assertIn("Flat by       18:40 IST", text)
        finally:
            exitp.SPECIAL_SESSIONS[day] = saved

    def test_flat_by_follows_the_brokers_square_off_for_an_fo_stock(self):
        self.write(os.path.join("nse", "entry.json"), REEL_ENTRY)
        for broker, flat, squareoff in (("zerodha", "15:07", "15:12"), ("dhan", "15:05", "15:10"),
                                        ("upstox", "15:05", "15:10"), ("groww", "15:00", "15:05")):
            self.write(os.path.join("nse", "config.json"), {"broker": broker})
            code, text, err = self.plan("--json")
            self.assertEqual(code, 0, err)
            p = json.loads(text)
            self.assertEqual((p["flat_by"], p["squareoff"], p["broker"], p["cas_stock"]),
                             (flat, squareoff, broker, True), broker)
        # the chosen broker is also the one whose brokerage the charges use
        self.write(os.path.join("nse", "config.json"), {"broker": "kotak"})
        kotak = json.loads(self.plan("--json")[1])["charges"]["at_stop"]
        self.write(os.path.join("nse", "config.json"), {"broker": "zerodha"})
        zerodha = json.loads(self.plan("--json")[1])["charges"]["at_stop"]
        self.assertLess(kotak, zerodha)               # Kotak's brokerage is capped at Rs 10 an order

    def test_the_plan_says_when_flat_by_was_lowered_and_prints_the_square_off(self):
        self.write(os.path.join("nse", "entry.json"), REEL_ENTRY)
        self.write(os.path.join("nse", "config.json"), {"broker": "dhan", "flat_by": "15:08"})
        code, text, _ = self.plan()
        self.assertEqual(code, 0)
        self.assertIn("Flat by       15:05 IST", text)
        self.assertIn("Square-off    15:10 IST: Dhan auto-squares-off open MIS F&O positions", text)
        self.assertIn("Flat-by lowered from 15:08 to 15:05", text)
        self.assertIn("Closing auction: RELIANCE is an F&O stock", text)
        # asking for a time at or after the broker's square-off is refused
        code, _, err = self.plan("--flat-by", "15:10")
        self.assertEqual(code, 1)
        self.assertIn("at or after 15:10, when Dhan will square off open MIS positions", err)
        # a time in the last 5 minutes before it is allowed, with a note
        code, text, _ = self.plan("--flat-by", "15:07")
        self.assertEqual(code, 0)
        self.assertIn("within 5 minutes of Dhan's 15:10 square-off", text)
        # delivery has no square-off
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, product="CNC"))
        p = json.loads(self.plan("--json")[1])
        self.assertIsNone(p["squareoff"])

    def test_an_unverified_brokers_square_off_comes_with_a_note(self):
        self.write(os.path.join("nse", "entry.json"), REEL_ENTRY)
        self.write(os.path.join("nse", "config.json"), {"broker": "sbi"})
        p = json.loads(self.plan("--json")[1])
        self.assertEqual((p["squareoff"], p["flat_by"]), ("15:00", "14:55"))
        self.assertTrue(any("not verified" in w for w in p["warnings"]))
        self.write(os.path.join("nse", "config.json"), {"broker": "sbi", "squareoff": {"cas": "15:12"}})
        p = json.loads(self.plan("--json")[1])
        self.assertEqual((p["squareoff"], p["flat_by"]), ("15:12", "15:07"))
        self.assertFalse(any("not verified" in w for w in p["warnings"]))
        self.write(os.path.join("nse", "config.json"), {"broker": "nobody"})
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("unknown broker", err)

    def test_the_switch_back_to_1530_on_2026_12_07(self):
        # continuous trading runs to 15:30 for every stock again: no F&O list is needed, no download
        for day, flat, cas in (("2026-12-04", "15:07", True), ("2026-12-07", "15:10", False)):
            self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, date=day))
            code, text, err = run(exitp, ["plan", "--now", day + " 10:01", "--json"])
            self.assertEqual(code, 0, err)
            p = json.loads(text)
            self.assertEqual((p["flat_by"], p["cas_stock"]), (flat, cas), day)
        self.assertEqual(len(self.fetched), 1)             # only the day before the switch looked at the list
        self.assertTrue(any("2026-12-07" in w and "square-off" in w for w in p["warnings"]))

    def test_the_built_in_muhurat_session_2026_11_08(self):
        day = "2026-11-08"
        outside = dict(REEL_ENTRY, symbol="AWL", yahoo="AWL.NS", date=day, time="2:40 PM")
        self.write(os.path.join("nse", "entry.json"), outside)
        code, text, err = run(exitp, ["plan", "--now", day + " 14:41", "--json"])
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["flat_by"], p["cas_stock"]), ("15:25", False))      # 15:30 close less 5 minutes
        self.assertTrue(any("Diwali Muhurat trading" in w for w in p["warnings"]))
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, date=day, time="2:40 PM"))
        p = json.loads(run(exitp, ["plan", "--now", day + " 14:41", "--json"])[1])
        self.assertEqual((p["flat_by"], p["cas_stock"]), ("15:10", True))       # F&O: 15:15 less 5 minutes

    def test_the_fo_list_comes_from_a_file_or_is_downloaded_once(self):
        self.write(os.path.join("nse", "entry.json"), REEL_ENTRY)
        with open("lots.csv", "w") as fh:
            fh.write("UNDERLYING,SYMBOL,OCT-26\nTCS,TCS,175\n")
        p = json.loads(self.plan("--fo-lots", "lots.csv", "--json")[1])
        self.assertEqual((p["cas_stock"], p["flat_by"], self.fetched), (False, "15:10", []))
        self.plan("--json")
        self.plan("--json")
        self.assertEqual(len(self.fetched), 1)
        today = exitp.to_ist(exitp.utc_now()).date().isoformat()
        self.assertTrue(os.path.exists(os.path.join("nse", "state", "fo_mktlots-%s.csv" % today)))

    def test_closed_day_and_calendar_warnings(self):
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, date="2026-10-20"))   # Dussehra
        code, text, _ = self.plan("--json")
        w = json.loads(text)["warnings"]
        self.assertTrue(any("NSE is closed on 2026-10-20 (NSE holiday: Dussehra)" in x for x in w))
        self.assertTrue(any("not today" in x for x in w))
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, date="2028-03-01"))
        code, text, _ = self.plan("--json")
        self.assertTrue(any("covers 2026" in x for x in json.loads(text)["warnings"]))

    def test_show_without_a_plan_and_with_a_broken_one(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)
        self.write(os.path.join("nse", "plan.json"), {"symbol": "RELIANCE"})
        code, _, err = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("is missing", err)


class SkillDoc(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "nse-exit", "SKILL.md"), encoding="utf-8") as fh:
            self.text = fh.read()

    def test_skill_md(self):
        text = self.text
        self.assertTrue(text.startswith("---\nname: nse-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertIn("## Safety", text)
        low = " ".join(text.lower().split())
        for must in ("on windows, use `py` in place of `python3`", "before the entry",
                     "a stop is a plan, not a guarantee", "day trading loses most people money", "/nse-timemachine",
                     "/nse-budget", "long side only", "₹1,183.65", "₹1,208.50", "₹9.95", "1213.50",
                     "too tight to survive the spread", "0.05 tick", "nse/plan.json", "15:10", "mis", "cnc"):
            self.assertIn(must, low)
        for never in ("—", "tick " + "by tick", "real " + "time", "pro" + "fit", "nyse", "new york", "alpaca"):
            self.assertNotIn(never, low)

    def test_every_plan_field_is_documented(self):
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(d)
        try:
            os.mkdir("nse")
            with open(os.path.join("nse", "entry.json"), "w") as fh:
                json.dump(REEL_ENTRY, fh)
            code, text, err = run(exitp, ["plan", "--now", "2026-10-26 10:01", "--stop", "atr:1.5", "--offline", REL, "--json"])
            self.assertEqual(code, 0, err)
            plan = json.loads(text)
            plan.pop("warnings")
            for k in list(plan) + list(plan["charges"]):
                self.assertIn('"%s"' % k, self.text, k)
        finally:
            os.chdir(cwd)
            shutil.rmtree(d)


class Leftovers(unittest.TestCase):
    def test_no_us_leftovers_in_the_script(self):
        with open(os.path.join(ROOT, "nse-exit", "exit.py"), encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("NYSE", "New York", "Eastern", "eastern", "trader/", "Alpaca", "day-trader-skill", "$%", "15:55"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
