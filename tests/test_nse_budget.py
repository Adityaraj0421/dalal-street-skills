import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("nse-budget", "size")
check = load("nse-risk", "check")
ticket = load("nse-broker", "ticket")

CFG = os.path.join(ROOT, "nse-budget", "config.example.json")
FO_LOTS = os.path.join(FIX, "_shared", "fo_mktlots.csv")
PLAN = os.path.join(FIX, "nse-budget", "plan-reliance.json")
TARGETS = os.path.join(ROOT, "nse-budget", "targets.example.json")
ORDERS_FIX = os.path.join(FIX, "nse-risk", "orders-reliance.json")
NOW = "2026-10-26 09:45"
TODAY = "2026-10-26"
PRICES = {"RELIANCE": 1198.0, "HDFCBANK": 960.5, "TCS": 3120.4, "INFY": 1480.2, "ITC": 410.15}


def cfg(**over):
    with open(CFG, encoding="utf-8") as fh:
        c = json.load(fh)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN, encoding="utf-8") as fh:
        p = json.load(fh)
    p.update(over)
    return p


_STATE = tempfile.mkdtemp(prefix="nse-budget-state-")


def run(argv):
    """size.main, offline: the F&O list comes from the fixture and nothing is
    cached into the repo, unless a test passes its own --fo-lots/--state-dir."""
    argv = list(argv)
    if argv and argv[0] not in ("-h", "--help") and "--fo-lots" not in argv and "--offline" not in argv:
        argv += ["--fo-lots", FO_LOTS]
    if argv and "--state-dir" not in argv:
        argv += ["--state-dir", _STATE]
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def prices_doc(price, quote_time="2026-10-26T04:12:13Z"):
    return {"as_of": quote_time, "source": "yahoo", "prices": {"RELIANCE": price},
            "detail": {"RELIANCE": {"prev_close": 1190.0, "time": quote_time, "source": "yahoo"}}}


class TheReliance(unittest.TestCase):
    """Rs 2,00,000, 10% buffer, Rs 1,000 max loss incl. charges, RELIANCE risk 7.60 -> 118 shares."""

    def test_config_example_keys(self):
        with open(CFG, encoding="utf-8") as fh:
            c = json.load(fh)
        self.assertEqual(c["budget"], 200000)
        self.assertEqual((c["max_loss_per_trade_inr"], c["max_daily_loss_inr"], c["mis_leverage"]), (1000, 3000, 5))
        self.assertEqual((c["no_entries_before"], c["no_entries_after"], c["flat_by"]), ("09:30", "14:45", "15:10"))
        self.assertEqual((c["broker"], c["paper"]), ("zerodha", True))
        self.assertTrue(all("usd" not in k for k in c))

    def test_sizing(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (118, "max loss"))
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 118), ("position cap", 418), ("max order", 334), ("cash", 752)])
        self.assertEqual((s["entry"], s["stop"], s["target"], s["risk_per_share"]), (1196.10, 1188.50, 1211.30, 7.60))
        self.assertEqual(s["margin_needed"], round(118 * 1196.10 / 5, 2))
        self.assertEqual(s["cash_buffer"], 20000)
        self.assertIsNone(s["refusal"])

    def test_max_loss_includes_round_trip_charges(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        ch = size.trade_charges(118 * 1196.10, 118 * 1188.50, "MIS", "NSE", None)["total"]
        self.assertEqual(s["charges_at_stop"], ch)
        self.assertEqual(s["max_loss"], round(118 * 7.60 + ch, 2))
        self.assertLessEqual(s["max_loss"], 1000)
        # one share more would break the max loss, charges included
        over, _ = size.loss_at(119, 1196.10, 1188.50, "MIS", "NSE", cfg())
        self.assertGreater(over, 1000)
        # without charges 131 shares would have fit: the charges decided it
        self.assertEqual(int(1000 // 7.60), 131)

    def test_reward_risk_is_net_of_charges_on_both_outcomes(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        net = 118 * 15.20 - s["charges_at_target"]
        self.assertAlmostEqual(s["reward_risk"], round(net / s["max_loss"], 2), places=2)
        self.assertLess(s["reward_risk"], 2.0)       # the plan says 2R before charges

    def test_broker_profile_charges_are_pasted_and_overridable(self):
        a = size.size_trade(plan(), cfg(), today=TODAY)
        b = size.size_trade(plan(), cfg(charges={"brokerage_cap_mis": 0.0, "brokerage_pct_mis": 0.0}), today=TODAY)
        self.assertLess(b["charges_at_stop"] / b["qty"], a["charges_at_stop"] / a["qty"])

    def test_order_contract(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        o = size.build_order(s, size.to_ist(size.parse_now(NOW)))
        self.assertEqual(o["id"], "RELIANCE-20261026-0945")
        self.assertEqual((o["symbol"], o["exchange"], o["yahoo"], o["series"], o["product"]),
                         ("RELIANCE", "NSE", "RELIANCE.NS", "EQ", "MIS"))
        self.assertEqual(o["entry"], {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                                      "transaction_type": "BUY", "quantity": 118, "product": "MIS",
                                      "order_type": "LIMIT", "price": 1196.1, "validity": "DAY"})
        self.assertEqual(o["stop"], {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                                     "transaction_type": "SELL", "quantity": 118, "product": "MIS",
                                     "order_type": "SL-M", "trigger_price": 1188.5, "validity": "DAY"})
        self.assertEqual(o["target"]["price"], 1211.3)
        self.assertIsNone(o["gtt_oco"])
        self.assertEqual(o["plan"]["r_per_share"], 7.6)
        self.assertEqual((o["flat_by"], o["margin_needed"]), ("15:07", s["margin_needed"]))   # F&O stock: Zerodha 15:12 - 5
        # broker-neutral fields at the order level
        self.assertEqual((o["side"], o["qty"], o["order_type"], o["price"], o["stop_trigger"], o["target_price"]),
                         ("BUY", 118, "LIMIT", 1196.1, 1188.5, 1211.3))
        self.assertEqual(o["broker"], "zerodha")
        self.assertEqual(o["fingerprint"], size.fingerprint(o))
        self.assertIn("bound by your ₹1,000 max loss", o["why"])

    def test_fingerprint_is_shared_and_sensitive(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        o = size.build_order(s, None)
        self.assertEqual(size.fingerprint(o), check.fingerprint(o))
        self.assertEqual(size.fingerprint(o), ticket.fingerprint(o))
        self.assertEqual(size.fingerprint(dict(o, why="anything")), size.fingerprint(o))
        edited = json.loads(json.dumps(o))
        edited["entry"]["quantity"] = 119
        self.assertNotEqual(size.fingerprint(edited), size.fingerprint(o))
        edited = json.loads(json.dumps(o))
        edited["stop"]["trigger_price"] = 1180.0
        self.assertNotEqual(size.fingerprint(edited), size.fingerprint(o))
        self.assertNotEqual(size.fingerprint(dict(o, qty=119)), size.fingerprint(o))

    def test_the_fixture_orders_file_is_what_it_writes(self):
        with open(ORDERS_FIX, encoding="utf-8") as fh:
            fixture = json.load(fh)
        self.assertEqual(fixture["created_by"], "nse-budget")
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual(size.build_order(s, size.to_ist(size.parse_now(NOW))), fixture["orders"][0])

    def test_cli_writes_the_order_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "nse", "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("Size: 118 shares of RELIANCE = ₹1,41,139.80 order value", text)
            self.assertIn("₹28,227.96 margin at 5x leverage", text)
            self.assertIn("Charges (what the government and the broker take)", text)
            self.assertIn("<- binds", text)
            self.assertIn("not financial advice", text)
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual((doc["created_by"], doc["status"], doc["created_at"]), ("nse-budget", "proposed", "2026-10-26T04:15:00Z"))
            self.assertIs(doc["paper"], True)
            self.assertEqual(len(doc["orders"]), 1)

    def test_cli_json(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["sizing"]["qty"], 118)
        self.assertEqual(out["orders"][0]["entry"]["order_type"], "LIMIT")


class PlanRules(unittest.TestCase):
    def test_a_stop_too_far_is_a_plain_refusal(self):
        s = size.size_trade(plan(stop=100.0), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("more than your ₹1,000 max loss per trade", s["refusal"])
        self.assertIsNone(size.build_order(s))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(plan(stop=100.0), fh)
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", p, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order:", text)
            self.assertFalse(os.path.exists(out))

    def test_each_limit_can_bind(self):
        self.assertEqual(size.size_trade(plan(), cfg(max_order_inr=100000), today=TODAY)["bound_by"], "max order")
        s = size.size_trade(plan(), cfg(max_position_pct=10), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (83, "position cap"))
        s = size.size_trade(plan(), cfg(), cash=25000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"]), (20, "cash", 5000))
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=5000), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (334, "max order"))

    def test_leverage_changes_the_margin_not_the_loss(self):
        a = size.size_trade(plan(), cfg(mis_leverage=1, max_position_pct=100), today=TODAY)
        self.assertEqual(a["margin_needed"], a["value"])
        self.assertEqual(a["qty"], 118)           # still bound by the max loss

    def test_a_fresh_higher_price_means_a_bigger_risk(self):
        s = size.size_trade(plan(), cfg(min_reward_risk=0), price=1198.02, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"]), (1198.05, "price", 9.55))
        self.assertLess(s["qty"], 118)
        s = size.size_trade(plan(), cfg(), price=1198.02, today=TODAY)
        lower = size.size_trade(plan(), cfg(), price=1190.0, today=TODAY)
        self.assertEqual((lower["entry"], lower["qty"]), (1196.10, 118))
        self.assertIsNone(lower["refusal"])

    def test_late_entry_fails_the_net_reward_risk(self):
        s = size.size_trade(plan(), cfg(min_reward_risk=1.5), price=1201.0, today=TODAY)
        self.assertIn("reward:risk", s["refusal"])
        self.assertIn("after charges", s["refusal"])
        self.assertIsNone(size.build_order(s))

    def test_prices_file_is_used_only_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump(prices_doc(1198.02), fh)
            base = ["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"), "--prices", pp, "--json"]
            out = json.loads(run(base + ["--now", "2026-10-26 09:50"])[1])
            self.assertEqual(out["sizing"]["entry_from"], "price")
            self.assertIn("above the plan's entry", out["notes"][0])
            out = json.loads(run(base + ["--now", "2026-10-26 11:30"])[1])
            self.assertEqual(out["sizing"]["entry_from"], "plan")
            self.assertIn("not used", out["notes"][0])

    def test_ticks(self):
        s = size.size_trade(plan(entry=1196.12, stop=1188.53, target=1211.34), cfg(), today=TODAY)
        self.assertEqual((s["entry"], s["stop"], s["target"]), (1196.15, 1188.50, 1211.30))
        s = size.size_trade(plan(), cfg(), limit=1195.97, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"]), (1196.0, "limit"))

    def test_the_portfolio_cash_caps_the_size(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "portfolio.json")
            with open(pf, "w") as fh:
                json.dump({"cash": 25000, "positions": []}, fh)
            out = json.loads(run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--now", NOW, "--json"])[1])
            self.assertEqual((out["sizing"]["qty"], out["sizing"]["bound_by"]), (20, "cash"))

    def test_bad_plans(self):
        for p, word in ((plan(stop=1200.0), "not below the entry"),
                        (plan(target=1190.0), "already happened"),
                        (plan(target=None), "positive target"),
                        (plan(side="sell"), "long (buy) trades only"),
                        (plan(symbol=""), "no symbol"),
                        (plan(series="BE"), "trade-for-trade"),
                        (plan(symbol="NIFTY", yahoo="^NSEI"), "index"),
                        (plan(product="NRML"), "MIS (intraday) or CNC")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(p, cfg(), today=TODAY)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), price=1220.0, today=TODAY)
        self.assertIn("already happened", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(blocked=["reliance"]), today=TODAY)
        self.assertIn("blocked list", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), today="2026-10-27")
        self.assertIn("make a new plan", str(e.exception))

    def test_be_series_is_fine_for_delivery(self):
        s = size.size_trade(plan(series="BE"), cfg(), today=TODAY, product="CNC")
        self.assertEqual(s["product"], "CNC")

    def test_under_the_minimum_order(self):
        s = size.size_trade(plan(entry=10.0, stop=9.0, target=12.0), cfg(max_loss_per_trade_inr=20, min_order_inr=500),
                            today=TODAY)
        self.assertGreater(s["qty"], 0)
        self.assertLess(s["value"], 500)
        self.assertIn("under your ₹500 minimum order", s["refusal"])

    def test_errors_are_one_line(self):
        code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG, "--now", NOW])
        self.assertEqual((code, "no plan" in err), (1, True))
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--top", "2"])
        self.assertEqual(code, 1)
        self.assertIn("--weights", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "tomorrow"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


class DeliveryCNC(unittest.TestCase):
    def test_cnc_uses_the_gtt_oco_and_delivery_charges(self):
        s = size.size_trade(plan(), cfg(), today=TODAY, product="CNC")
        self.assertEqual((s["product"], s["leverage"], s["flat_by"]), ("CNC", 1.0, None))
        self.assertEqual(s["margin_needed"], s["value"])
        self.assertLessEqual(s["max_loss"], 1000)
        self.assertGreater(s["charges_breakdown"]["dp_charge"], 0)       # the DP charge on the sale
        self.assertEqual(s["charges_breakdown"]["brokerage"], 0)         # delivery brokerage is free
        o = size.build_order(s, size.to_ist(size.parse_now(NOW)))
        self.assertIsNone(o["stop"])
        self.assertEqual(o["entry"]["product"], "CNC")
        g = o["gtt_oco"]
        self.assertEqual((g["trigger_type"], g["trigger_values"], g["tradingsymbol"], g["exchange"]),
                         ("two-leg", [1188.5, 1211.3], "RELIANCE", "NSE"))
        self.assertEqual([x["price"] for x in g["orders"]], [1188.5, 1211.3])
        self.assertTrue(all(x["transaction_type"] == "SELL" and x["quantity"] == s["qty"] for x in g["orders"]))
        self.assertIsNone(o["flat_by"])
        self.assertEqual(o["stop_trigger"], 1188.5)

    def test_cli_product_flag(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--product", "cnc", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["orders"][0]["gtt_oco"]["trigger_type"], "two-leg")


class Allocation(unittest.TestCase):
    """CNC portfolio mode: Rs 5 lakh across Nifty names, whole shares."""

    def rows(self):
        with open(TARGETS, encoding="utf-8") as fh:
            return size.parse_weights(json.load(fh))

    def test_allocation_in_rupees_and_whole_shares(self):
        r = size.size(self.rows(), cfg(budget=500000, max_position_pct=25), prices=PRICES)
        self.assertEqual(r["cash_buffer"], 50000)
        self.assertEqual(r["investable"], 450000)
        for p in r["positions"]:
            self.assertEqual(p["shares"], int(p["shares"]))
            self.assertAlmostEqual(p["inr"], p["shares"] * PRICES[p["ticker"]], places=2)
            self.assertLessEqual(p["inr"], 125000)
        self.assertAlmostEqual(r["invested"] + r["idle"], 450000, places=2)
        self.assertLess(r["idle"], max(PRICES.values()) + 1)
        self.assertGreater(r["buy_charges"], 0)
        self.assertEqual(r["positions"][0]["ticker"], "RELIANCE")

    def test_cap_redistributes(self):
        w = size.parse_weights({"RELIANCE": 60, "TCS": 30, "ITC": 10})
        r = size.size(w, cfg(budget=1000000, cash_buffer_pct=0, max_position_pct=40, min_order_inr=1), prices=PRICES)
        self.assertEqual(sorted(p["ticker"] for p in r["positions"] if p["capped"]), ["RELIANCE", "TCS"])
        for p in r["positions"]:
            self.assertLessEqual(p["inr"], 400000)

    def test_dropped_blocked_and_small(self):
        r = size.size(self.rows(), cfg(budget=500000, blocked=["itc"]), prices=PRICES)
        self.assertNotIn("ITC", [p["ticker"] for p in r["positions"]])
        self.assertIn("blocked", r["dropped"][0]["reason"])
        r = size.size(size.parse_weights({"RELIANCE": 95, "TCS": 5}), cfg(budget=100000, min_order_inr=5000,
                                                                       max_position_pct=100), prices=PRICES)
        self.assertEqual([p["ticker"] for p in r["positions"]], ["RELIANCE"])
        self.assertIn("TCS", [d["ticker"] for d in r["dropped"]])

    def test_prices_are_required(self):
        with self.assertRaises(size.SizeError) as e:
            size.size(self.rows(), cfg(budget=500000), prices={"RELIANCE": 1198.0})
        self.assertIn("need a price for", str(e.exception))
        with self.assertRaises(size.SizeError):
            size.size(self.rows(), {"cash_buffer_pct": 10}, prices=PRICES)

    def test_orders_are_cnc_contract_orders(self):
        r = size.size(self.rows(), cfg(budget=500000), prices=PRICES)
        orders = size.orders_from(r, "an example", cfg(), size.to_ist(size.parse_now(NOW)))
        o = orders[0]
        self.assertEqual((o["product"], o["entry"]["order_type"], o["entry"]["transaction_type"]), ("CNC", "LIMIT", "BUY"))
        self.assertEqual(o["entry"]["price"], size.tick_up(PRICES["RELIANCE"]))
        self.assertIsNone(o["stop"])
        self.assertIsNone(o["gtt_oco"])
        self.assertEqual(o["qty"], o["entry"]["quantity"])
        self.assertEqual(o["fingerprint"], size.fingerprint(o))
        self.assertEqual(o["id"], "RELIANCE-20261026-ALLOC")

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"prices": {"RELIANCE.NS": 1198.0, "HDFCBANK": 960.5, "TCS": 3120.4, "INFY": 1480.2,
                                      "ITC": 410.15}}, fh)
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(cfg(budget=500000), fh)
            out = os.path.join(d, "nse", "orders.json")
            code, text, _ = run(["--targets", TARGETS, "--config", c, "--prices", pp, "--write", out, "--now", NOW])
            self.assertEqual(code, 0)
            self.assertIn("left idle", text)
            self.assertIn("buy-side charges", text)
            self.assertIn("₹", text)
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual([o["symbol"] for o in doc["orders"]], ["RELIANCE", "HDFCBANK", "TCS", "INFY", "ITC"])
            code, _, err = run(["--targets", "/nonexistent/targets.json", "--config", c, "--prices", pp])
            self.assertEqual(code, 1)
            self.assertIn("no weights file", err)

    def test_symbols_accept_yahoo_style(self):
        w = size.parse_weights({"reliance.ns": 50, "NSE:TCS": 50})
        self.assertEqual([r["ticker"] for r in w], ["RELIANCE", "TCS"])


class RealChain(unittest.TestCase):
    """plan.json written by the real nse-exit/exit.py and prices.json by nse-alerts/alerts.py (saved fixtures)."""
    REAL_PLAN = os.path.join(FIX, "nse-budget", "plan-real-exit.json")
    REAL_PRICES = os.path.join(FIX, "nse-budget", "prices-real-alerts.json")
    NOW_REAL = "2026-10-26 10:28"

    def test_the_real_plan_and_prices_flow_through_budget_risk_and_broker(self):
        with open(self.REAL_PLAN, encoding="utf-8") as fh:
            real = json.load(fh)
        self.assertEqual((real["created_by"], real["product"], real["flat_by"], real["yahoo"], real["exchange"]),
                         ("nse-exit", "MIS", "15:10", "RELIANCE.NS", "NSE"))
        self.assertIn("at_stop", real["charges"])
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "nse", "orders.json")
            code, text, _ = run(["--plan", self.REAL_PLAN, "--config", CFG, "--prices", self.REAL_PRICES,
                                 "--portfolio", os.path.join(d, "none"), "--now", self.NOW_REAL, "--write", out,
                                 "--offline", "--fo-lots", FO_LOTS])
            self.assertEqual(code, 0, text)
            self.assertIn("above the plan's entry", text)            # the fresh alerts quote (1197.20) was used
            with open(out, encoding="utf-8") as fh:
                o = json.load(fh)["orders"][0]
            self.assertEqual((o["product"], o["flat_by"], o["yahoo"], o["exchange"], o["entry"]["price"]),
                             ("MIS", "15:07", "RELIANCE.NS", "NSE", 1197.2))     # the plan's 15:10 is cut to the CAS flat_by
            self.assertEqual(o["id"], "RELIANCE-20261026-1028")       # entry_time is null in a planned-ahead plan
            sec = os.path.join(FIX, "nse-risk", "sec_list.csv")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = check.main(["--orders", out, "--config", CFG, "--portfolio", none, "--prices", self.REAL_PRICES,
                                 "--state", none, "--offline", "--seclist", sec, "--fo-lots", FO_LOTS, "--now", self.NOW_REAL,
                                 "--out", os.path.join(d, "risk.json")])
            self.assertEqual(rc, 0, buf.getvalue())
            risk = os.path.join(d, "risk.json")
            state = os.path.join(d, "broker.json")
            pf = os.path.join(d, "portfolio.json")
            targs = ["--orders", out, "--config", CFG, "--risk", risk, "--state", state, "--portfolio", pf,
                     "--now", self.NOW_REAL]
            with redirect_stdout(io.StringIO()) as sbuf:
                self.assertEqual(ticket.main(["show"] + targs), 0)
                self.assertEqual(ticket.main(["approve", "--index", "1", "--reply", "yes"] + targs), 0)
            with open(pf, encoding="utf-8") as fh:
                pos = json.load(fh)["positions"][0]
            self.assertEqual((pos["ticker"], pos["product"], pos["flat_by"], pos["stop"]), ("RELIANCE", "MIS", "15:07", 1188.5))

    def test_alerts_price_keys_nse_by_symbol_and_bse_with_suffix(self):
        doc = {"as_of": "2026-10-26T04:58:00Z", "prices": {"RELIANCE": 1197.2, "RELIANCE.BO": 1250.0},
               "detail": {"RELIANCE": {"time": "2026-10-26T04:57:20Z", "yahoo": "RELIANCE.NS", "exchange": "NSE"},
                          "RELIANCE.BO": {"time": "2026-10-26T04:57:20Z", "yahoo": "RELIANCE.BO", "exchange": "BSE"}}}
        now = size.parse_now(self.NOW_REAL)
        self.assertEqual(size.fresh_price(doc, "RELIANCE", now, "RELIANCE.NS")[0], 1197.2)
        self.assertEqual(size.fresh_price(doc, "RELIANCE", now, "RELIANCE.BO")[0], 1250.0)
        p, times, prevs = check.load_prices(doc)
        self.assertEqual(p, {"RELIANCE": 1197.2, "RELIANCE.BO": 1250.0})


class BrokerProfiles(unittest.TestCase):
    def test_zerodha_is_the_default_and_the_core_has_ten(self):
        p = size.broker_profile({})
        self.assertEqual((p["key"], p["squareoff"]), ("zerodha", {"cas": "15:12", "other": "15:25"}))
        self.assertEqual(sorted(size.BROKERS), sorted(["groww", "zerodha", "angelone", "icicidirect", "upstox", "kotak",
                                                       "hdfcsky", "dhan", "sbi", "motilal"]))
        with open(CFG, encoding="utf-8") as fh:
            c = json.load(fh)
        self.assertEqual(c["broker"], "zerodha")
        for k in size.BROKERS:
            self.assertIn(k, c["_comment_broker"])

    def test_unknown_broker_is_refused(self):
        with self.assertRaises(size.IndiaError) as e:
            size.size_trade(plan(), cfg(broker="someone"), today=TODAY)
        self.assertIn("unknown broker 'someone'", str(e.exception))
        code, _, err = run(["--plan", PLAN, "--config", self._cfg_file(broker="someone"), "--now", NOW, "--offline",
                            "--fo-lots", FO_LOTS, "--portfolio", "/nonexistent"])
        self.assertEqual(code, 1)
        self.assertIn("unknown broker", err)

    def _cfg_file(self, **over):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "config.json")
        with open(path, "w") as fh:
            json.dump(cfg(**over), fh)
        return path

    def test_one_known_charges_case_per_broker(self):
        # MIS round trip, Rs 10,000 bought and sold: brokerage is the broker's own (statutory charges are shared)
        mis = {"groww": 20.0, "zerodha": 6.0, "angelone": 20.0, "icicidirect": 10.0, "upstox": 20.0, "kotak": 10.0,
               "hdfcsky": 40.0, "dhan": 6.0, "sbi": 40.0, "motilal": 4.0}
        for k, want in mis.items():
            ch = size.trade_charges(10000, 10000, "MIS", "NSE", {"broker": k})
            self.assertEqual(ch["brokerage"], want, k)
        # statutory part is identical everywhere
        z = size.trade_charges(10000, 10000, "MIS", "NSE", {"broker": "zerodha"})
        self.assertEqual((z["stt"], z["stamp_duty"]), (2.5, 0.3))
        # delivery: brokerage on Rs 10,000 each way, and the DP charge on the sale
        cnc = {"groww": (20.0, 23.6), "zerodha": (0.0, 15.34), "angelone": (20.0, 23.6), "upstox": (40.0, 23.6),
               "kotak": (40.0, 23.6), "dhan": (0.0, 14.75), "sbi": (70.0, 23.6), "motilal": (40.0, 18.5)}
        for k, (brk, dp) in cnc.items():
            ch = size.trade_charges(10000, 10000, "CNC", "NSE", {"broker": k})
            self.assertEqual((ch["brokerage"], ch["dp_charge"]), (brk, dp), k)

    def test_sizing_uses_the_configured_brokers_charges(self):
        z = size.size_trade(plan(), cfg(), today=TODAY)
        g = size.size_trade(plan(), cfg(broker="hdfcsky"), today=TODAY)
        self.assertEqual(z["charges_at_stop"], 97.02)
        self.assertEqual(g["broker"], "hdfcsky")
        self.assertEqual(g["charges_at_stop"], size.trade_charges(g["qty"] * 1196.1, g["qty"] * 1188.5, "MIS", "NSE",
                                                                  cfg(broker="hdfcsky"))["total"])
        o = size.build_order(g)
        self.assertEqual(o["broker"], "hdfcsky")

    def test_user_charges_win_over_the_profile(self):
        a = size.trade_charges(10000, 10000, "MIS", "NSE", {"broker": "groww"})
        b = size.trade_charges(10000, 10000, "MIS", "NSE", {"broker": "groww", "charges": {"gst_pct": 0}})
        self.assertGreater(a["gst"], 0)
        self.assertEqual(b["gst"], 0)

    def test_flat_by_is_per_broker_stock_and_day(self):
        # CAS (F&O) stock vs a non-F&O stock, Zerodha, before 2026-12-07
        cas = size.size_trade(plan(), cfg(), today=TODAY, cas_stock=True)
        non = size.size_trade(plan(), cfg(), today=TODAY, cas_stock=False)
        self.assertEqual((cas["flat_by"], cas["squareoff"]), ("15:07", "15:12"))
        self.assertEqual((non["flat_by"], non["squareoff"]), ("15:10", "15:25"))     # the config's 15:10 wins
        self.assertTrue(cas["cas_stock"])
        # the plan's own flat_by can only make it earlier
        early = size.size_trade(plan(flat_by="14:30"), cfg(), today=TODAY, cas_stock=False)
        self.assertEqual(early["flat_by"], "14:30")
        late = size.size_trade(plan(flat_by="15:19"), cfg(), today=TODAY, cas_stock=False)
        self.assertEqual(late["flat_by"], "15:10")
        # another broker: Dhan squares off equity at 15:10 -> 15:05
        d = size.size_trade(plan(), cfg(broker="dhan"), today=TODAY, cas_stock=False)
        self.assertEqual((d["flat_by"], d["squareoff"]), ("15:05", "15:10"))
        # an unverified broker prints the note
        k = size.size_trade(plan(), cfg(broker="kotak"), today=TODAY, cas_stock=False)
        self.assertIn("not verified", k["squareoff_note"])
        # CNC has no flat_by
        self.assertIsNone(size.size_trade(plan(), cfg(), today=TODAY, product="CNC")["flat_by"])

    def test_the_dec_7_switch(self):
        before = size.size_trade(plan(date="2026-12-04"), cfg(), today="2026-12-04", cas_stock=True)
        after = size.size_trade(plan(date="2026-12-08"), cfg(), today="2026-12-08", cas_stock=True)
        self.assertEqual((before["flat_by"], before["squareoff"]), ("15:07", "15:12"))
        self.assertEqual((after["flat_by"], after["squareoff"]), ("15:10", "15:25"))
        self.assertIn("before SEBI's 2026-12-07 auction change", after["squareoff_note"])
        self.assertIsNone(before["squareoff_note"])

    def test_cli_shows_the_square_off_and_uses_the_fo_list(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--offline", "--fo-lots", FO_LOTS])
        self.assertEqual(code, 0)
        self.assertIn("Flat by 15:07 IST", text)
        self.assertIn("Zerodha auto-squares-off MIS at 15:12", text)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(plan(symbol="ABAN", yahoo="ABAN.NS", series="EQ"), fh)       # not in the F&O fixture (ABAN)
            code, text, _ = run(["--plan", p, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                                 "--offline", "--fo-lots", FO_LOTS])
            self.assertIn("Flat by 15:10 IST", text)


class FoListCache(unittest.TestCase):
    def test_cache_rules(self):
        d1 = datetime.date(2026, 10, 26)
        with open(FO_LOTS, encoding="utf-8") as fh:
            text = fh.read()
        with tempfile.TemporaryDirectory() as sd:
            fo, note = size.load_fo(sd, d1, offline=True)
            self.assertEqual((fo, "unavailable" in note), (None, True))
            self.assertTrue(size.is_cas_stock(None, "ANYTHING"))                 # unknown = the cautious side
            with open(size.fo_cache_file(sd, datetime.date(2026, 10, 22)), "w", encoding="utf-8") as fh:
                fh.write(text)                                                    # 4 days old
            fo, note = size.load_fo(sd, d1, offline=True)
            self.assertIn("RELIANCE", fo)
            self.assertIn("cached 4 days ago", note)
            with open(size.fo_cache_file(sd, datetime.date(2026, 10, 10)), "w", encoding="utf-8") as fh:
                fh.write(text)
            os.remove(size.fo_cache_file(sd, datetime.date(2026, 10, 22)))
            self.assertIsNone(size.load_fo(sd, d1, offline=True)[0])            # 16 days old: too old
            with open(size.fo_cache_file(sd, d1), "w", encoding="utf-8") as fh:
                fh.write(text)
            fo, note = size.load_fo(sd, d1, offline=True)
            self.assertEqual((note, size.is_cas_stock(fo, "reliance"), size.is_cas_stock(fo, "ABAN")), (None, True, False))
            self.assertNotIn("NIFTY", fo)                                         # indexes are left out


if __name__ == "__main__":
    unittest.main()
