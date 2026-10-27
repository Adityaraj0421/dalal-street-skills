import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("nse-broker", "ticket")
check = load("nse-risk", "check")

CFG_PATH = os.path.join(ROOT, "nse-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "nse-broker", "orders-reliance.json")
SEC_PATH = os.path.join(FIX, "nse-risk", "sec_list.csv")
FO_PATH = os.path.join(FIX, "_shared", "fo_mktlots.csv")
PLAN_PATH = os.path.join(FIX, "nse-budget", "plan-reliance.json")
NOW = "2026-10-26 09:45"
TODAY = "2026-10-26"
TATA = {"symbol": "TCS", "exchange": "NSE", "product": "CNC", "side": "BUY", "qty": 10, "order_type": "LIMIT",
        "price": 3120.4}


def reliance(**over):
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


KITE_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
             "quantity": 118, "product": "MIS", "order_type": "LIMIT", "price": 1196.1, "validity": "DAY"}
BATCH = ["RELIANCE", "TCS", "INFY"]


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=None, batch=BATCH):
        return ticket.parse_yes(reply, order or reliance(), batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes RELIANCE", "yes 118 RELIANCE",
                  "yes 1188.50", "yes target 1211.30", "yes max loss 993.82", "yes 1,196.10"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "go", "do it", "yeah", "yep", "absolutely", "send it",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both",
                  "yes TCS", "yes 100", "yes 119 RELIANCE", "yes but make it 100", "yes?", "no", "not yes",
                  "yes, wait", "yes if it is under 1190", "yes stop at 1180"):
            self.assertFalse(self.yes(r), r)

    def test_the_reason_is_given(self):
        ok, why = ticket.parse_yes("yes TCS", reliance(), BATCH)
        self.assertIn("names TCS", why)
        ok, why = ticket.parse_yes("yes 100", reliance(), BATCH)
        self.assertIn("but this order is BUY 118 RELIANCE", why)


class TicketShape(unittest.TestCase):
    def test_tool_input_is_the_kite_place_order_call(self):
        self.assertEqual(ticket.tool_input(ticket.entry_of(reliance())), KITE_CALL)
        o = reliance()
        o["entry"] = dict(o["entry"], extra="x")
        self.assertEqual(ticket.tool_input(o["entry"]), KITE_CALL)
        name, payload, notes = ticket.entry_payload(reliance(), {})
        self.assertEqual((name, payload), ("place_order", KITE_CALL))

    def test_plain_english(self):
        parts = ticket.plain_parts(reliance())
        self.assertEqual(parts[0], "BUY 118 RELIANCE (MIS intraday), LIMIT 1196.10, valid today")
        self.assertEqual(parts[1], "stop-loss SL-M trigger 1188.50 (a SELL placed after the fill)")
        self.assertEqual(parts[2], "target 1211.30")
        self.assertEqual(parts[3], "max loss about ₹993.82 if the stop fills at 1188.50, charges included "
                                   "(it can fill worse in a fast market)")
        self.assertEqual(parts[4], "flat by 15:07 IST")
        self.assertEqual(ticket.max_loss(reliance()), (993.82, 1188.5))

    def test_cnc_ticket_shows_the_gtt(self):
        size = load("nse-budget", "size")
        with open(CFG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
        with open(PLAN_PATH, encoding="utf-8") as fh:
            plan = json.load(fh)
        o = size.build_order(size.size_trade(plan, cfg, today=TODAY, product="CNC"), None)
        text = ticket.plain(o)
        self.assertIn("(CNC delivery)", text)
        self.assertIn("GTT OCO after the fill: sell at the stop 1188.50 or the target 1211.30", text)
        self.assertEqual(ticket.stop_of(o), 1188.5)
        self.assertEqual(ticket.target_of(o), 1211.3)

    def test_neutral_orders_without_legs(self):
        e = ticket.entry_of(TATA)
        self.assertEqual((e["tradingsymbol"], e["transaction_type"], e["quantity"], e["product"]),
                         ("TCS", "BUY", 10, "CNC"))
        self.assertEqual(ticket.symbol_of(TATA), "TCS")
        self.assertEqual(ticket.neutral_payload(TATA)["quantity"], 10)

    def test_fingerprint_is_the_same_everywhere(self):
        o = reliance()
        self.assertEqual(ticket.fingerprint(o), check.fingerprint(o))
        self.assertNotEqual(ticket.fingerprint(dict(o, qty=119)), ticket.fingerprint(o))


class PaperBase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def set_cfg(self, **over):
        with open(self.cfg, encoding="utf-8") as fh:
            c = json.load(fh)
        c.update(over)
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump(c, fh)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--portfolio", self.pf, "--now", kw.get("now", NOW)]

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--now", now, "--out", self.risk,
                               "--offline", "--seclist", SEC_PATH, "--fo-lots", FO_PATH])

    def state_doc(self):
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)

    def pf_doc(self):
        with open(self.pf, encoding="utf-8") as fh:
            return json.load(fh)


class Flow(PaperBase):
    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_the_paper_flow_to_the_target(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER ledger, no real money, Zerodha", text)
        for part in ticket.plain_parts(reliance()):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: nse-risk PASS", text)
        self.assertIn("charges (government and broker): about ₹97.02 round trip at the stop, ₹97.78 at the target", text)
        self.assertIn("Paper mode", text)
        self.assertNotIn("place_order", text)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(os.path.exists(self.pf))

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes", "--price", "1195"))
        self.assertEqual(code, 0, text)
        self.assertIn("PAPER FILL (simulated, nothing sent to Zerodha): BUY 118 RELIANCE at ₹1,196.10 "
                      "(your limit price; the market was 1195.00, the --price you gave)", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        appr, plc, fil = st["orders"]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["date"], appr["product"]),
                         ("yes", "entry", 993.82, TODAY, "MIS"))
        self.assertEqual(appr["at"], "2026-10-26T09:45:00+05:30")
        self.assertTrue(plc["broker_order_id"].startswith("PAPER-"))
        self.assertEqual((fil["price"], fil["via"]), (1196.1, "paper"))
        pf = self.pf_doc()
        buy_ch = ticket.trade_charges(118 * 1196.1, 0, "MIS", "NSE", None)["total"]
        self.assertEqual(pf["cash"], round(200000 - 28227.96 - buy_ch, 2))
        pos = pf["positions"][0]
        self.assertEqual((pos["ticker"], pos["product"], pos["shares"], pos["stop"], pos["target"], pos["flat_by"]),
                         ("RELIANCE", "MIS", 118, 1188.5, 1211.3, "15:07"))
        self.assertEqual(pos["margin"], 28227.96)

        # a second yes for the same order is refused, and the log shows it
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("DONE: placed", err)
        code, text, _ = run(["log", "--state", self.state, "--portfolio", self.pf, "--now", NOW])
        self.assertIn("1 placed today", text)
        self.assertIn("max loss ₹993.82", text)

        # nse-risk counts it
        res = check.check([reliance()], {"budget": 200000}, state=st, now=check.parse_now("2026-10-26 10:30"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual((res["today"]["mis_entries_placed"], res["today"]["loss_at_risk"]), (1, 993.82))

        # not touched yet
        code, text, _ = run(["track", "--price", "RELIANCE=1195.0", "--portfolio", self.pf, "--config", self.cfg,
                             "--state", self.state, "--now", "2026-10-26 10:30", "--offline"])
        self.assertIn("not touched", text)
        # the target is hit
        code, text, _ = run(["track", "--price", "RELIANCE=1212", "--portfolio", self.pf, "--config", self.cfg,
                             "--state", self.state, "--now", "2026-10-26 11:30", "--offline"])
        self.assertEqual(code, 0)
        self.assertIn("TARGET hit", text)
        pf = self.pf_doc()
        self.assertEqual(pf["positions"], [])
        c = pf["closed"][0]
        sell_ch = ticket.trade_charges(0, 118 * 1211.3, "MIS", "NSE", None)["total"]
        self.assertEqual(c["gross"], round(118 * 1211.3 - 118 * 1196.1, 2))
        self.assertEqual(c["charges"], round(buy_ch + sell_ch, 2))
        self.assertEqual(c["net"], round(c["gross"] - c["charges"], 2))
        self.assertEqual(pf["cash"], round(200000 + c["net"], 2))
        self.assertEqual(self.state_doc()["orders"][-1]["via"], "paper target")
        code, text, _ = run(["log", "--state", self.state, "--portfolio", self.pf, "--now", "2026-10-26 15:00"])
        self.assertIn("Realised today: +₹1,793.60 before charges", text)

    def test_the_stop_hit_is_a_net_loss_bigger_than_the_gross(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes", "--price", "1195"))
        code, text, _ = run(["track", "--price", "RELIANCE=1185", "--portfolio", self.pf, "--config", self.cfg,
                             "--state", self.state, "--now", "2026-10-26 11:30", "--offline"])
        self.assertIn("STOP hit", text)
        self.assertIn("sold 118 at ₹1,185.00", text)            # already below the stop: no better fill is assumed
        self.assertIn("can be worse than this", text)
        c = self.pf_doc()["closed"][0]
        self.assertEqual(c["gross"], round(118 * (1185 - 1196.1), 2))
        self.assertLess(c["net"], c["gross"])

    def test_flatten_by_the_close_in_paper(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes", "--price", "1195"))
        late = "2026-10-26 15:12"
        code, text, _ = run(["track", "--offline", "--price", "RELIANCE=1200", "--portfolio", self.pf,
                             "--config", self.cfg, "--state", self.state, "--now", late])
        self.assertIn("past its flat_by 15:07 IST and still open", text)
        self.assertIn("Zerodha squares off MIS from about 15:12 with a fee", text)
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", late, "--limit-price", "1200"])
        self.assertEqual(code, 0, text)
        self.assertIn("SELL 118 RELIANCE (MIS intraday), LIMIT 1200.00, valid today", text)
        self.assertIn("replaced 1 earlier proposal", text)
        with open(self.orders, encoding="utf-8") as fh:
            doc = json.load(fh)
        o = doc["orders"][0]
        self.assertEqual((o["side"], o["qty"], o["product"]), ("SELL", 118, "MIS"))
        self.assertEqual(ticket.tool_input(o["entry"])["transaction_type"], "SELL")
        self.assertEqual(self.run_risk(now=late), 0)               # after the 14:45 cutoff: an exit still passes
        with open(self.risk, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["results"][0]["kind"], "exit")
        code, text, _ = run(self.args("show", now=late))
        self.assertIn("READY", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes sell 118 RELIANCE",
                                      "--price", "1200.5", now=late))
        self.assertEqual(code, 0, text)
        self.assertIn("Realised:", text)
        self.assertEqual(self.pf_doc()["positions"], [])
        self.assertEqual(len(self.pf_doc()["closed"]), 1)
        code, _, err = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_flatten_with_the_broker_qty_and_market_note(self):
        code, text, _ = run(["flatten", "--symbol", "reliance", "--qty", "118", "--orders", self.orders,
                             "--portfolio", self.none, "--plan", self.none, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("MARKET order", text)
        self.assertNotIn("Check it against", text)
        self.assertEqual(run(["flatten", "--symbol", "RELIANCE", "--qty", "0", "--orders", self.orders])[0], 1)
        self.assertEqual(run(["flatten", "--symbol", "RELIANCE", "--qty", "1.5", "--orders", self.orders])[0], 1)

    def test_a_paper_market_exit_needs_a_price(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes", "--price", "1195"))
        run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
             "--config", self.cfg, "--now", "2026-10-26 15:00"])
        self.run_risk(now="2026-10-26 15:00")
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-26 15:00"))
        self.assertEqual(code, 1)
        self.assertIn("needs a price", err)
        self.assertEqual(len(self.pf_doc()["positions"]), 1)
        self.assertEqual([r["status"] for r in self.state_doc()["orders"]].count("approved"), 1)   # no yes was booked for the exit
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes", "--price", "1201.4",
                                      now="2026-10-26 15:00"))
        self.assertEqual(code, 0, text)
        self.assertIn("at ₹1,201.40 (the --price you gave)", text)

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        with open(self.orders, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["orders"][0]["entry"]["quantity"] = 119
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.run_risk(now="2026-10-23 10:15")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-10-26 09:20"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-26 09:20"))
        self.assertIn("BLOCKED by nse-risk: window: no new entries before 09:30", text)
        code, _, _ = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-26 09:20"))
        self.assertEqual(code, 1)

    def test_decline_is_logged(self):
        self.run_risk()
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        self.assertIn("Nothing was sent", text)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)

    def test_the_paper_ledger_refuses_what_the_cash_cannot_cover(self):
        with open(self.pf, "w", encoding="utf-8") as fh:
            json.dump({"cash": 20000, "positions": []}, fh)
        # nse-risk already blocks it, so force the ledger step with a fill
        code, _, err = run(["fill", "--symbol", "RELIANCE", "--side", "sell", "--product", "MIS", "--qty", "5",
                            "--price", "1200", "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertIn("the portfolio holds 0", err)
        self.assertNotIn("Traceback", err)
        with self.assertRaises(ticket.TicketError) as e:
            ticket.apply_fill(self.pf, {"budget": 1, "mis_leverage": 5}, "RELIANCE", "buy", 118, 1196.1, TODAY,
                              "MIS", "NSE", strict=True)
        self.assertIn("not enough cash in the paper ledger", str(e.exception))

    def test_live_mode_prints_the_place_order_json_and_legs(self):
        self.set_cfg(paper=False)
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("LIVE ACCOUNT, REAL MONEY", text)
        self.assertIn("How this reaches the market: Zerodha's hosted MCP (https://mcp.kite.trade/mcp) is read-only plus GTT", text)
        self.assertIn("Entry payload (place_order):", text)
        self.assertIn("static IP", text)
        self.assertIn("10 orders a second", text)
        self.assertIn("Algo ID", text)
        self.assertIn('"order_type": "LIMIT"', text)
        self.assertIn("Allow once", text)
        self.assertIn("ticket.py legs", text)
        self.assertNotIn("Paper mode", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text[text.index("{"):text.index("}") + 1]), KITE_CALL)
        self.assertFalse(os.path.exists(self.pf))                   # nothing was booked
        self.assertEqual([r["status"] for r in self.state_doc()["orders"]], ["approved"])
        code, _, err = run(self.args("legs", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("record the entry fill first", err)

        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "2610260000123",
                                      "--filled-qty", "118", "--filled-price", "1196.10", now="2026-10-26 09:46"))
        self.assertEqual(code, 0)
        self.assertIn("has NO stop at the broker yet", text)
        self.assertEqual([r["status"] for r in self.state_doc()["orders"]], ["approved", "placed", "filled"])
        self.assertEqual(self.state_doc()["orders"][1]["broker_order_id"], "2610260000123")
        self.assertEqual(self.pf_doc()["positions"][0]["shares"], 118)

        code, text, _ = run(self.args("legs", "--index", "1"))
        self.assertEqual(code, 0)
        stop = json.loads(text[text.index("{"):text.index("}") + 1])
        self.assertEqual((stop["order_type"], stop["trigger_price"], stop["quantity"], stop["transaction_type"]),
                         ("SL-M", 1188.5, 118, "SELL"))
        self.assertIn("There is no OCO for an MIS stop", text)
        self.assertIn("SHORT", text)
        code, text, _ = run(self.args("legs", "--index", "1", "--qty", "60"))
        self.assertIn('"quantity": 60', text)

    def test_live_result_after_a_paper_config_is_refused(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("paper ledger", err)

    def test_live_result_needs_an_approval(self):
        self.set_cfg(paper=False)
        self.run_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)

    def test_cnc_legs_are_the_gtt_oco(self):
        self.set_cfg(paper=False)
        size = load("nse-budget", "size")
        with open(CFG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
        with open(PLAN_PATH, encoding="utf-8") as fh:
            plan = json.load(fh)
        s = size.size_trade(plan, cfg, today=TODAY, product="CNC")
        o = size.build_order(s, size.to_ist(size.parse_now(NOW)))
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump({"orders": [o]}, fh)
        self.assertEqual(self.run_risk(), 0)
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", str(s["qty"]),
                                      "--filled-price", "1196.10"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("legs", "--index", "1"))
        self.assertEqual(code, 0, text)
        g = json.loads(text[text.index("{"):text.rindex("}") + 1])
        self.assertEqual((g["trigger_type"], g["trigger_values"]), ("two-leg", [1188.5, 1211.3]))
        self.assertTrue(all(x["quantity"] == s["qty"] for x in g["orders"]))
        self.assertIn("do not guess its name", text)
        self.assertNotIn("flat_by", self.pf_doc()["positions"][0])

    def test_unknown_broker_is_refused_by_every_command(self):
        self.set_cfg(broker="somebroker", paper=False)
        for argv in (self.args("show"), self.args("approve", "--index", "1", "--reply", "yes"),
                     ["fill", "--symbol", "RELIANCE", "--side", "buy", "--product", "MIS", "--qty", "1", "--price", "1",
                      "--config", self.cfg, "--portfolio", self.pf, "--state", self.state],
                     ["flatten", "--symbol", "RELIANCE", "--qty", "1", "--config", self.cfg, "--orders", self.orders],
                     ["track", "--offline", "--config", self.cfg, "--portfolio", self.pf, "--state", self.state]):
            code, _, err = run(argv)
            self.assertEqual(code, 1, argv[0])
            self.assertIn("unknown broker 'somebroker'", err)
            self.assertNotIn("Traceback", err)

    def test_show_json(self):
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1", "--json"))
        j = json.loads(text)
        self.assertEqual((j["ready"], j["tool"], j["paper"], j["broker"]), (True, "place_order", True, "zerodha"))
        self.assertEqual(j["tool_input"], KITE_CALL)
        self.assertEqual(j["fingerprint"], ticket.fingerprint(reliance()))


def chart(bars):
    """A Yahoo chart document from [(hh, mm, open, high, low, close)] on 2026-10-26 (IST)."""
    ts = [int((datetime.datetime(2026, 10, 26, b[0], b[1]) - datetime.timedelta(hours=5, minutes=30))
              .replace(tzinfo=datetime.timezone.utc).timestamp()) for b in bars]
    cols = list(zip(*[b[2:] for b in bars]))
    return {"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS", "gmtoffset": 19800}, "timestamp": ts,
                                  "indicators": {"quote": [{"open": list(cols[0]), "high": list(cols[1]),
                                                            "low": list(cols[2]), "close": list(cols[3])}]}}]}}


class WorkingOrders(PaperBase):
    """A paper LIMIT behaves like a real day order: it fills only when the market is at or through it."""

    def quote(self, price, at_utc):
        path = os.path.join(self.d, "prices.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"prices": {"RELIANCE": price}, "detail": {"RELIANCE": {"time": at_utc}}}, fh)
        return path

    def bars(self, rows):
        path = os.path.join(self.d, "bars.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(chart(rows), fh)
        return path

    def working(self, now, *extra):
        return run(["working", "--offline", "--portfolio", self.pf, "--config", self.cfg, "--state", self.state,
                    "--prices", self.none, "--now", now] + list(extra))

    def approve_away_from_the_market(self):
        self.assertEqual(self.run_risk(), 0)
        prices = self.quote(1199.4, "2026-10-26T04:12:00Z")             # 09:42 IST, above the 1196.10 buy limit
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes RELIANCE", "--prices", prices))
        self.assertEqual(code, 0, text)
        return text

    def test_a_marketable_limit_fills_at_the_limit(self):
        self.assertEqual(self.run_risk(), 0)
        prices = self.quote(1194.8, "2026-10-26T04:10:00Z")             # 09:40 IST: fresh, below the buy limit
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes", "--prices", prices))
        self.assertEqual(code, 0, text)
        self.assertIn("BUY 118 RELIANCE at ₹1,196.10 (your limit price; the market was 1194.80, the fresh quote", text)
        self.assertEqual([r["status"] for r in self.state_doc()["orders"]], ["approved", "placed", "filled"])
        self.assertEqual(self.pf_doc()["positions"][0]["shares"], 118)
        self.assertTrue(ticket.marketable("buy", 1196.1, 1196.1))
        self.assertTrue(ticket.marketable("sell", 1200, 1200.5))
        self.assertFalse(ticket.marketable("sell", 1200, 1199.95))

    def test_a_stale_quote_does_not_fill(self):
        self.assertEqual(self.run_risk(), 0)
        prices = self.quote(1190.0, "2026-10-26T03:50:00Z")             # 09:20 IST: 25 minutes old
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes", "--prices", prices))
        self.assertEqual(code, 0, text)
        self.assertIn("NOT filled: there is no quote at most 15 minutes old", text)
        self.assertFalse(os.path.exists(self.pf))

    def test_a_limit_away_from_the_market_is_working_not_filled(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertNotIn("this ledger assumes it does", text)
        self.assertIn("only if\n  the market is at or through it", text)
        self.assertIn("expires unfilled at 14:30 IST (the entry cut-off", text)
        text = self.approve_away_from_the_market()
        self.assertIn("PAPER WORKING ORDER (simulated, nothing sent to Zerodha): BUY 118 RELIANCE LIMIT 1196.10 is "
                      "NOT filled: the market is 1199.40 (the fresh quote", text)
        self.assertIn("above your BUY limit 1196.10", text)
        self.assertFalse(os.path.exists(self.pf))                         # nothing in the ledger
        st = self.state_doc()["orders"]
        self.assertEqual([r["status"] for r in st], ["approved", "working"])
        w = st[1]
        self.assertEqual((w["reply"], w["fingerprint"], w["at"], w["limit"], w["expires"], w["cas_stock"]),
                         ("yes RELIANCE", ticket.fingerprint(reliance()), "2026-10-26T09:45:00+05:30", 1196.1,
                          "14:30", True))
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("WORKING since 2026-10-26T09:45:00+05:30", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("WORKING since", err)
        # nse-risk counts the resting order like a placed one
        res = check.check([reliance()], {"budget": 200000}, state=self.state_doc(),
                          now=check.parse_now("2026-10-26 10:00"))
        self.assertEqual(res["today"]["mis_entries_placed"], 1)
        code, text, _ = self.working("2026-10-26 10:00")
        self.assertIn("BUY 118 RELIANCE LIMIT 1196.10 (MIS, since 09:45): still WORKING until 14:30 IST, no price", text)

    def test_a_working_order_fills_when_a_bar_low_reaches_the_limit(self):
        self.approve_away_from_the_market()
        bars = self.bars([(9, 40, 1195, 1196, 1190, 1195),                # before the order: does not count
                          (9, 45, 1199, 1200, 1197.5, 1198), (9, 46, 1198, 1199, 1196.1, 1197),
                          (9, 47, 1197, 1198, 1193, 1194)])
        code, text, _ = self.working("2026-10-26 10:00", "--bars", "RELIANCE=" + bars)
        self.assertEqual(code, 0, text)
        self.assertIn("FILLED at ₹1,196.10 at 09:46 IST (the bars you gave since the order; the low was 1193.00)", text)
        st = self.state_doc()["orders"]
        self.assertEqual([r["status"] for r in st], ["approved", "working", "placed", "filled"])
        self.assertEqual((st[2]["filled_at"], st[3]["price"], st[3]["fingerprint"]),
                         ("2026-10-26T09:46:00+05:30", 1196.1, ticket.fingerprint(reliance())))
        pos = self.pf_doc()["positions"][0]
        self.assertEqual((pos["shares"], pos["stop"], pos["target"], pos["opened_at"]),
                         (118, 1188.5, 1211.3, "2026-10-26T09:46:00+05:30"))
        code, text, _ = self.working("2026-10-26 10:05")
        self.assertIn("No working paper orders", text)
        res = check.check([reliance()], {"budget": 200000}, state=self.state_doc(),
                          now=check.parse_now("2026-10-26 10:05"))
        self.assertEqual(res["today"]["mis_entries_placed"], 1)          # counted once, as placed

    def test_track_fills_a_working_order_then_tracks_the_position(self):
        self.approve_away_from_the_market()
        bars = self.bars([(9, 45, 1199, 1200, 1194, 1195), (9, 50, 1195, 1196, 1195.5, 1196)])
        code, text, _ = run(["track", "--offline", "--bars", "RELIANCE=" + bars, "--portfolio", self.pf,
                             "--config", self.cfg, "--state", self.state, "--now", "2026-10-26 09:55"])
        self.assertIn("FILLED at ₹1,196.10 at 09:45 IST", text)
        self.assertIn("stop 1188.50 and target 1211.30 not touched (the bars you gave, since the entry)", text)

    def test_a_working_order_expires_at_the_cut_off_with_no_ledger_change(self):
        self.approve_away_from_the_market()
        bars = self.bars([(9, 45, 1199, 1201, 1197, 1200), (14, 30, 1199, 1199, 1190, 1191)])   # after the cut-off
        code, text, _ = self.working("2026-10-26 14:29", "--bars", "RELIANCE=" + bars)
        self.assertIn("still WORKING until 14:30 IST, the low was 1197.00", text)
        code, text, _ = self.working("2026-10-26 14:31", "--bars", "RELIANCE=" + bars)
        self.assertIn("EXPIRED unfilled at 14:30 IST (the entry cut-off: continuous trading ends at 15:15, minus "
                      "your 45-minute entry buffer); the low was 1197.00", text)
        self.assertIn("The ledger is unchanged", text)
        self.assertFalse(os.path.exists(self.pf))
        st = self.state_doc()["orders"]
        self.assertEqual([r["status"] for r in st], ["approved", "working", "expired"])
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-26 14:31"))
        self.assertIn("DONE: expired", text)
        res = check.check([reliance()], {"budget": 200000}, state=self.state_doc(),
                          now=check.parse_now("2026-10-26 14:31"))
        self.assertEqual(res["today"]["mis_entries_placed"], 0)
        code, text, _ = self.working("2026-10-26 14:40")
        self.assertIn("No working paper orders", text)

    def test_the_expiry_times(self):
        cfg = {"no_entries_after": "14:45", "entry_buffer_min": 45}
        self.assertEqual(ticket.working_expiry(TODAY, "MIS", True, cfg)[0], datetime.time(14, 30))
        self.assertEqual(ticket.working_expiry(TODAY, "MIS", False, cfg)[0], datetime.time(14, 45))
        t, why = ticket.working_expiry(TODAY, "MIS", False, dict(cfg, no_entries_after="14:00"))
        self.assertEqual((t, why), (datetime.time(14, 0), "your no_entries_after"))
        self.assertEqual(ticket.working_expiry(TODAY, "CNC", True, cfg), (datetime.time(15, 30), "the close"))
        self.assertIsNone(ticket.working_expiry("2026-10-25", "CNC", True, cfg)[0])     # a Sunday

class Ledger(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.pf = os.path.join(self.d, "portfolio.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_cnc_buy_and_sell_relieve_the_oldest_lot_with_charges(self):
        pf = {"cash": 100000, "positions": [{"ticker": "TCS", "product": "CNC", "shares": 3, "lots": [
            {"id": "TCS-1", "date": "2025-01-02", "shares": 2, "cost": 6000.0, "charges": 6.0},
            {"id": "TCS-2", "date": "2026-01-02", "shares": 1, "cost": 3100.0, "charges": 3.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        res = ticket.apply_fill(self.pf, {}, "TCS", "sell", 2.0, 3200.0, TODAY, "CNC", "NSE")
        sell_ch = ticket.trade_charges(0, 6400.0, "CNC", "NSE", None)
        self.assertGreater(sell_ch["dp_charge"], 0)
        self.assertEqual(res["gross"], 400.0)
        self.assertEqual(res["net"], round(400.0 - 6.0 - sell_ch["total"], 2))
        out = res["portfolio"]
        self.assertEqual(out["cash"], round(100000 + 6400.0 - sell_ch["total"], 2))
        self.assertEqual([l["id"] for l in out["positions"][0]["lots"]], ["TCS-2"])
        with self.assertRaises(ticket.TicketError):
            ticket.apply_fill(self.pf, {}, "TCS", "sell", 5, 3200.0, TODAY, "CNC", "NSE")

    def test_mis_and_cnc_positions_are_kept_apart(self):
        ticket.apply_fill(self.pf, {"budget": 500000}, "TCS", "buy", 10, 3000.0, TODAY, "CNC", "NSE")
        ticket.apply_fill(self.pf, {"budget": 500000}, "TCS", "buy", 10, 3000.0, TODAY, "MIS", "NSE")
        with open(self.pf, encoding="utf-8") as fh:
            pf = json.load(fh)
        self.assertEqual(sorted((p["product"], p["margin"]) for p in pf["positions"]), [("CNC", 30000.0), ("MIS", 6000.0)])
        with self.assertRaises(ticket.TicketError):
            ticket.apply_fill(self.pf, {}, "TCS", "sell", 15, 3000.0, TODAY, "MIS", "NSE")

    def test_cli_fill_records_stop_and_target(self):
        state = os.path.join(self.d, "broker.json")
        code, text, _ = run(["fill", "--symbol", "INFY", "--side", "buy", "--product", "MIS", "--qty", "50",
                             "--price", "1480", "--stop", "1470", "--target", "1500", "--flat-by", "15:10",
                             "--date", TODAY, "--portfolio", self.pf, "--config", CFG_PATH, "--state", state,
                             "--now", NOW])
        self.assertEqual(code, 0, text)
        with open(self.pf, encoding="utf-8") as fh:
            p = json.load(fh)["positions"][0]
        self.assertEqual((p["stop"], p["target"], p["flat_by"], p["margin"]), (1470.0, 1500.0, "15:10", 14800.0))
        code, text, _ = run(["fill", "--symbol", "INFY", "--side", "sell", "--product", "MIS", "--qty", "50",
                             "--price", "1490", "--date", TODAY, "--portfolio", self.pf, "--config", CFG_PATH,
                             "--state", state])
        self.assertIn("Realised:", text)
        with open(state, encoding="utf-8") as fh:
            self.assertEqual([r["status"] for r in json.load(fh)["orders"]], ["filled", "filled"])


class Tracking(unittest.TestCase):
    POS = {"ticker": "RELIANCE", "stop": 1188.5, "target": 1211.3}

    def test_price_hits(self):
        self.assertIsNone(ticket.evaluate_price(self.POS, 1200))
        self.assertEqual(ticket.evaluate_price(self.POS, 1188.5), {"hit": "stop", "price": 1188.5})
        self.assertEqual(ticket.evaluate_price(self.POS, 1180), {"hit": "stop", "price": 1180.0})
        self.assertEqual(ticket.evaluate_price(self.POS, 1215), {"hit": "target", "price": 1211.3})

    def bar(self, hh, mm, o, h, l, c):
        return {"t": datetime.datetime(2026, 10, 26, hh, mm), "open": o, "high": h, "low": l, "close": c}

    def test_bars_first_touch_wins_and_a_both_bar_is_a_stop(self):
        bars = [self.bar(9, 45, 1196, 1199, 1194, 1198), self.bar(9, 50, 1198, 1205, 1196, 1204),
                self.bar(9, 55, 1204, 1212, 1203, 1210)]
        r = ticket.evaluate_bars(self.POS, bars)
        self.assertEqual((r["hit"], r["price"], r["at"].minute), ("target", 1211.3, 55))
        both = bars + [self.bar(10, 0, 1210, 1213, 1187, 1190)]
        self.assertEqual(ticket.evaluate_bars(self.POS, both)["hit"], "target")      # earlier bar
        wild = [self.bar(10, 0, 1200, 1213, 1187, 1190)]
        self.assertEqual(ticket.evaluate_bars(self.POS, wild)["hit"], "stop")
        gap = [self.bar(10, 0, 1180, 1185, 1178, 1183)]
        self.assertEqual(ticket.evaluate_bars(self.POS, gap)["price"], 1180.0)       # a gap fills at the open
        after = datetime.datetime(2026, 10, 26, 9, 57)
        self.assertIsNone(ticket.evaluate_bars(self.POS, bars, after))

    def test_bars_from_a_yahoo_chart(self):
        base = int(datetime.datetime(2026, 10, 26, 3, 45).replace(tzinfo=datetime.timezone.utc).timestamp())   # 09:15 IST
        doc = {"chart": {"result": [{"meta": {"gmtoffset": 19800}, "timestamp": [base, base + 300],
                                     "indicators": {"quote": [{"open": [1, 2], "high": [3, 4], "low": [0.5, None],
                                                               "close": [2, 3]}]}}]}}
        bars = ticket.bars_from_chart(doc)
        self.assertEqual(len(bars), 1)                                # the bar with a None is skipped
        self.assertEqual(bars[0]["t"], datetime.datetime(2026, 10, 26, 9, 15))
        self.assertEqual(ticket.bars_from_chart({}), [])


class BrokerTickets(unittest.TestCase):
    """One ticket per broker, from the neutral order (field names: docs/brokers.md)."""

    def cfg(self, key, paper=False):
        return {"broker": key, "paper": paper}

    def cnc(self):
        o = reliance(product="CNC")
        o["entry"] = dict(o["entry"], product="CNC")
        o["stop"] = None
        o["flat_by"] = None
        o["gtt_oco"] = {"trigger_type": "two-leg", "tradingsymbol": "RELIANCE", "exchange": "NSE",
                        "trigger_values": [1188.5, 1211.3], "last_price": 1196.1,
                        "orders": [{"transaction_type": "SELL", "quantity": 118, "order_type": "LIMIT", "price": 1188.5},
                                   {"transaction_type": "SELL", "quantity": 118, "order_type": "LIMIT", "price": 1211.3}]}
        return o

    def test_the_ten_profiles_come_from_the_core(self):
        self.assertEqual(sorted(ticket.RENDERERS), sorted(ticket.BROKERS))
        self.assertEqual(ticket.broker_profile({})["name"], "Zerodha")

    def test_zerodha(self):
        tool, p, notes = ticket.entry_payload(reliance(), self.cfg("zerodha"))
        self.assertEqual((tool, p), ("place_order", KITE_CALL))
        self.assertTrue(any("verified" in n for n in notes))
        tool, p, _ = ticket.render_leg(reliance(), self.cfg("zerodha"), "stop", 60)
        self.assertEqual((p["order_type"], p["trigger_price"], p["quantity"]), ("SL-M", 1188.5, 60))

    def test_groww(self):
        tool, p, _ = ticket.entry_payload(reliance(), self.cfg("groww"))
        self.assertEqual(p, {"trading_symbol": "RELIANCE", "quantity": 118, "price": 1196.1, "validity": "DAY",
                             "exchange": "NSE", "segment": "CASH", "product": "MIS", "order_type": "LIMIT",
                             "transaction_type": "BUY"})
        _, s, _ = ticket.render_leg(reliance(), self.cfg("groww"), "stop", 118)
        self.assertEqual((s["order_type"], s["trigger_price"], s["transaction_type"]), ("SL_M", 1188.5, "SELL"))

    def test_angelone_values_are_strings_and_flagged(self):
        tool, p, notes = ticket.entry_payload(reliance(), self.cfg("angelone"))
        self.assertEqual((tool, p["variety"], p["producttype"], p["ordertype"], p["quantity"], p["price"],
                          p["tradingsymbol"], p["transactiontype"]),
                         ("placeOrder", "NORMAL", "INTRADAY", "LIMIT", "118", "1196.10", "RELIANCE-EQ", "BUY"))
        self.assertTrue(p["symboltoken"].startswith("<"))
        self.assertTrue(any("UNVERIFIED" in n for n in notes))
        _, s, _ = ticket.render_leg(reliance(), self.cfg("angelone"), "stop", 118)
        self.assertEqual((s["variety"], s["ordertype"], s["triggerprice"]), ("STOPLOSS", "STOPLOSS_MARKET", "1188.50"))
        _, c, _ = ticket.entry_payload(self.cnc(), self.cfg("angelone"))
        self.assertEqual(c["producttype"], "DELIVERY")

    def test_icicidirect_mis_is_manual_and_cnc_uses_breeze(self):
        tool, p, notes = ticket.entry_payload(reliance(), self.cfg("icicidirect"))
        self.assertIsNone(tool)
        self.assertEqual(p["quantity"], 118)                       # the neutral order, to type by hand
        self.assertIn("does not allow intraday", notes[0])
        tool, p, notes = ticket.entry_payload(self.cnc(), self.cfg("icicidirect"))
        self.assertEqual((tool, p["product"], p["action"], p["order_type"], p["exchange_code"], p["validity"]),
                         ("place_order", "cash", "buy", "limit", "NSE", "day"))
        self.assertTrue(p["stock_code"].startswith("<"))

    def test_upstox(self):
        tool, p, notes = ticket.entry_payload(reliance(), self.cfg("upstox"))
        self.assertEqual((tool, p["product"], p["order_type"], p["transaction_type"], p["quantity"], p["validity"]),
                         ("POST /v3/order/place", "I", "LIMIT", "BUY", 118, "DAY"))
        self.assertNotIn("market_protection", p)
        self.assertTrue(p["instrument_token"].startswith("<"))
        _, s, _ = ticket.render_leg(reliance(), self.cfg("upstox"), "stop", 118)
        self.assertEqual((s["order_type"], s["trigger_price"], s["market_protection"]), ("SL-M", 1188.5, -1))
        _, c, _ = ticket.entry_payload(self.cnc(), self.cfg("upstox"))
        self.assertEqual(c["product"], "D")

    def test_kotak_is_flagged_unverified(self):
        tool, p, notes = ticket.entry_payload(reliance(), self.cfg("kotak"))
        self.assertEqual((p["exchange_segment"], p["product"], p["order_type"], p["transaction_type"], p["quantity"],
                          p["price"], p["amo"]), ("nse_cm", "MIS", "L", "B", "118", "1196.10", "NO"))
        self.assertTrue(any("UNVERIFIED" in n for n in notes))

    def test_hdfcsky_is_manual_because_the_fields_are_unknown(self):
        tool, p, notes = ticket.entry_payload(reliance(), self.cfg("hdfcsky"))
        self.assertIsNone(tool)
        self.assertIn("could not be verified", notes[0])
        self.assertEqual(p["tradingsymbol"], "RELIANCE")

    def test_dhan(self):
        tool, p, _ = ticket.entry_payload(reliance(), self.cfg("dhan"))
        self.assertEqual((tool, p["transactionType"], p["exchangeSegment"], p["productType"], p["orderType"],
                          p["quantity"], p["price"], p["validity"]),
                         ("POST /v2/orders", "BUY", "NSE_EQ", "INTRADAY", "LIMIT", 118, 1196.1, "DAY"))
        self.assertTrue(p["securityId"].startswith("<"))
        _, s, _ = ticket.render_leg(reliance(), self.cfg("dhan"), "stop", 118)
        self.assertEqual((s["orderType"], s["triggerPrice"]), ("STOP_LOSS_MARKET", 1188.5))
        _, c, _ = ticket.entry_payload(self.cnc(), self.cfg("dhan"))
        self.assertEqual(c["productType"], "CNC")

    def test_sbi_is_manual(self):
        tool, p, notes = ticket.entry_payload(reliance(), self.cfg("sbi"))
        self.assertIsNone(tool)
        self.assertIn("no public retail API", notes[0])
        self.assertEqual(ticket.live_route(ticket.broker_profile(self.cfg("sbi")))[0], "manual")

    def test_motilal_flags_its_gaps(self):
        tool, p, notes = ticket.entry_payload(reliance(), self.cfg("motilal"))
        self.assertEqual((p["exchange"], p["buyorsell"], p["ordertype"], p["producttype"], p["orderduration"],
                          p["price"], p["quantityinlot"], p["amoorder"]),
                         ("NSE", "BUY", "LIMIT", "VALUEPLUS", "DAY", 1196.1, 118, "N"))
        self.assertIn("algoid", p)
        self.assertTrue(any("UNVERIFIED" in n for n in notes))
        _, s, snotes = ticket.render_leg(reliance(), self.cfg("motilal"), "stop", 118)
        self.assertEqual((s["ordertype"], s["price"], s["triggerprice"]), ("STOPLOSS", 1188.5, 1188.5))

    def test_every_broker_renders_a_show_ticket_with_the_sebi_note(self):
        for k in ticket.BROKERS:
            text, ready = ticket.render(reliance(), 1, 1, self.cfg(k), {"date": TODAY, "results": [
                {"fingerprint": ticket.fingerprint(reliance()), "verdict": "PASS", "kind": "entry",
                 "reasons": [], "warnings": []}]}, {"orders": []}, TODAY)
            self.assertTrue(ready, k)
            self.assertIn("ORDER 1 of 1, LIVE ACCOUNT, REAL MONEY, " + ticket.BROKERS[k]["name"], text)
            self.assertIn("static IP", text, k)
            self.assertIn("How this reaches the market:", text, k)
            tj = ticket.ticket_json(reliance(), 1, self.cfg(k), None, {"orders": []}, TODAY)
            self.assertEqual(tj["broker"], k)

    def test_live_routes_follow_the_research(self):
        route = lambda k: ticket.live_route(ticket.broker_profile(self.cfg(k)))
        self.assertEqual(route("groww")[0], "places")
        self.assertIn("places orders", route("groww")[1])
        self.assertEqual(route("zerodha")[0], "gtt_only")
        self.assertIn("self-hosted", route("zerodha")[1])
        self.assertEqual(route("upstox")[0], "read_only")
        self.assertEqual(route("kotak")[0], "read_only")
        self.assertIn("cannot place orders", route("kotak")[1])
        self.assertEqual(route("hdfcsky")[0], "unknown")
        for k in ("angelone", "icicidirect", "dhan", "motilal"):
            self.assertEqual(route(k)[0], "none", k)
            self.assertIn("no official MCP", route(k)[1])

    def test_cnc_stop_is_a_gtt_in_words_except_for_zerodha_json(self):
        g, notes = ticket.gtt_text(self.cnc(), self.cfg("zerodha"), 60)
        self.assertEqual((g["trigger_values"], [x["quantity"] for x in g["orders"]]), ([1188.5, 1211.3], [60, 60]))
        g, notes = ticket.gtt_text(self.cnc(), self.cfg("dhan"), 60)
        self.assertIsNone(g)
        self.assertIn("Forever Order of type OCO", notes[0])
        g, notes = ticket.gtt_text(self.cnc(), self.cfg("groww"), 60)
        self.assertIn("GTT with an OCO", notes[0])

    def test_paper_stays_the_default_for_every_broker(self):
        for k in ticket.BROKERS:
            self.assertTrue(ticket.is_paper({"broker": k}), k)
            text, _ = ticket.render(reliance(), 1, 1, {"broker": k}, None, {"orders": []}, TODAY)
            self.assertIn("PAPER ledger", text, k)
            self.assertNotIn("static IP", text, k)


class Cli(unittest.TestCase):
    def test_help_and_errors(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ticket.main([]), 1)
        self.assertIn("usage", out.getvalue())
        code, _, err = run(["show", "--orders", "/nonexistent/orders.json"])
        self.assertEqual(code, 1)
        self.assertIn("no orders file", err)
        self.assertNotIn("Traceback", err)
        code, _, err = run(["show", "--orders", ORDERS_PATH, "--now", "noon"])
        self.assertEqual(code, 1)
        self.assertIn("--now wants", err)
        code, _, err = run(["log", "--state", "/nonexistent/s.json", "--today", "monday"])
        self.assertEqual(code, 1)
        self.assertIn("--today wants", err)
        code, text, _ = run(["log", "--state", "/nonexistent/s.json", "--now", NOW, "--portfolio", "/nonexistent/p.json"])
        self.assertEqual(code, 1)
        self.assertIn("No decisions recorded today", text)


if __name__ == "__main__":
    unittest.main()
