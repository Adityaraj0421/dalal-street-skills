import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

deals = load("nse-bigmoney", "deals")
DIR = os.path.join(FIX, "nse-bigmoney")
NOW = "2026-10-26 18:30"
HEADER = "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"
NO_BLOCK = "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\nNO RECORDS,,,,,,\n"


def read(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = deals.main(args)
    return code, out.getvalue(), err.getvalue()


class Sandbox(unittest.TestCase):
    """A temp folder with bulk.csv / block.csv (real 2026-10-23 bulk file, no block deals)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.off = os.path.join(self.tmp, "src")
        os.makedirs(self.off)
        self.state = os.path.join(self.tmp, "nse", "state", "deals.json")
        self.put(read("bulk-2026-10-23.csv"), NO_BLOCK)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, bulk, block):
        for n, t in (("bulk.csv", bulk), ("block.csv", block)):
            with open(os.path.join(self.off, n), "w", encoding="utf-8", newline="") as fh:
                fh.write(t)

    def go(self, *args):
        base = ["--offline", self.off, "--state", self.state, "--now", NOW,
                "--portfolio", os.path.join(self.tmp, "nope.json"),
                "--watchlist", os.path.join(self.tmp, "nope2.json")]
        return run([args[0]] + list(args[1:]) + base)


class Parsing(unittest.TestCase):
    def setUp(self):
        self.rows = deals.parse_deals(read("bulk-2026-10-23.csv"), "bulk")

    def test_real_file(self):
        self.assertEqual(len(self.rows), 118)
        d = self.rows[0]
        self.assertEqual((d["date"], d["symbol"], d["side"]), ("2026-10-23", "BONFIG", "BUY"))
        self.assertEqual(d["client"], "CATAMARAN VENTURES LLP")
        self.assertEqual(d["qty"], 1045000)
        self.assertEqual(d["price"], 581.35)
        self.assertAlmostEqual(d["value"], 1045000 * 581.35, places=2)
        self.assertEqual(d["yahoo"], "BONFIG.NS")

    def test_double_spaces_in_names_collapse(self):
        d = self.rows[1]
        self.assertEqual(d["client"], "EQUISM CAPITAL PRIVATE LIMITED")

    def test_ids_are_unique_and_stable(self):
        ids = [d["id"] for d in self.rows]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(ids, [d["id"] for d in deals.parse_deals(read("bulk-2026-10-23.csv"), "bulk")])

    def test_no_records(self):
        self.assertEqual(deals.parse_deals(NO_BLOCK, "block"), [])

    def test_not_a_deal_file(self):
        with self.assertRaises(deals.DealsError):
            deals.parse_deals("<html>Access Denied</html>", "bulk")

    def test_block_layout_without_remarks(self):
        rows = deals.parse_deals(read("block-synthetic.csv"), "block")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["client"], "SYNTHETIC SELLER PRIVATE LIMITED")
        self.assertEqual(rows[0]["kind"], "block")

    def test_identical_rows_stay_distinct(self):
        text = HEADER + "23-OCT-2026,AAA,A Ltd,X FUND,BUY,100,10.00,-\n" * 2
        rows = deals.parse_deals(text, "bulk")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])

    def test_date(self):
        self.assertEqual(deals.parse_nse_date("03-JAN-2027"), "2027-01-03")
        self.assertIsNone(deals.parse_nse_date("2027-01-03"))
        self.assertIsNone(deals.parse_nse_date("31-FEB-2027"))


class Selection(unittest.TestCase):
    def setUp(self):
        self.rows = deals.parse_deals(read("bulk-2026-10-23.csv"), "bulk")

    def test_client_substring_is_case_insensitive(self):
        got = deals.select(self.rows, clients=["invesco mutual"])
        self.assertEqual({d["client"] for d in got}, {"INVESCO MUTUAL FUND"})
        self.assertTrue(got)

    def test_combine_adds_quantity_and_weights_price(self):
        got = [d for d in deals.combine(deals.select(self.rows, clients=["invesco"], symbols=["SMT"]))]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["qty"], 800000 + 2807366)
        self.assertEqual(got[0]["rows"], 2)
        expect = (800000 * 374.91 + 2807366 * 371.44) / (800000 + 2807366)
        self.assertAlmostEqual(got[0]["price"], expect, places=2)

    def test_min_crore(self):
        got = deals.select(self.rows, min_crore=100)
        self.assertTrue(got)
        self.assertTrue(all(d["value"] >= 100 * 1e7 for d in got))

    def test_who_nets_buys_and_sells(self):
        res = deals.who(self.rows, ["GRT STRATEGIC"])
        smt = next(s for s in res["stocks"] if s["symbol"] == "SMT")
        self.assertEqual(smt["buy_qty"], 538038)
        self.assertEqual(smt["sell_qty"], 538038)
        self.assertLess(smt["net_value"], 0)       # sold slightly higher than bought: tiny net sale in value
        self.assertEqual(smt["net_qty"], 0)


class YourStocks(unittest.TestCase):
    def test_reads_both_files_in_any_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            pf = os.path.join(tmp, "portfolio.json")
            wl = os.path.join(tmp, "watchlist.json")
            with open(pf, "w") as fh:
                json.dump({"positions": [{"symbol": "RELIANCE", "qty": 5}, {"ticker": "TCS.NS"}]}, fh)
            with open(wl, "w") as fh:
                json.dump({"picks": [{"symbol": "TONBO", "yahoo": "TONBO.NS"}], "tickers": ["SMT", "TCS"]}, fh)
            mine = deals.my_stocks(pf, wl, ["infy"])
        self.assertEqual(mine["RELIANCE"], "holding")
        self.assertEqual(mine["TCS"], "holding")       # a holding beats a watchlist entry
        self.assertEqual(mine["TONBO"], "watchlist")
        self.assertEqual(mine["SMT"], "watchlist")
        self.assertEqual(mine["INFY"], "asked")

    def test_missing_files_are_fine(self):
        self.assertEqual(deals.my_stocks("/no/such", "/no/such/either"), {})


class Cli(Sandbox):
    def test_new_alerts_once(self):
        code, out, err = self.go("new")
        self.assertEqual(code, 0, err)
        self.assertIn("NEW deals: 118 rows", out)
        self.assertIn("₹", out)
        self.assertIn("crore", out)
        self.assertIn("Run this each trading evening", out)
        self.assertIn("not what comes next", out)
        code, out, err = self.go("new")
        self.assertEqual(code, 1)
        self.assertIn("Nothing new", out)

    def test_history_accumulates_across_days(self):
        self.go("new")
        day2 = HEADER + "24-OCT-2026,SMT,Sahajanand Medical Tech L,INVESCO MUTUAL FUND,BUY,100000,380.00,-\n"
        self.put(day2, read("block-synthetic.csv"))
        code, out, err = self.go("new")
        self.assertEqual(code, 0, err)
        self.assertIn("NEW deals: 4 rows", out)
        with open(self.state, encoding="utf-8") as fh:
            st = json.load(fh)
        self.assertEqual(sorted({d["date"] for d in st["deals"]}), ["2026-10-21", "2026-10-23", "2026-10-24"])
        self.assertEqual(len(st["deals"]), 118 + 1 + 3)

    def test_refetching_the_same_file_adds_nothing(self):
        self.go("new")
        self.go("new")
        with open(self.state, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["deals"]), 118)

    def test_client_filter_leaves_the_rest_unseen(self):
        code, out, err = self.go("new", "--client", "invesco")
        self.assertEqual(code, 0)
        self.assertIn("INVESCO MUTUAL FUND", out)
        self.assertNotIn("BONFIG", out)
        code, out, err = self.go("new")
        self.assertEqual(code, 0)
        self.assertIn("BONFIG", out)
        self.assertNotIn("INVESCO MUTUAL FUND", out)

    def test_flags_holdings_and_watchlist(self):
        pf = os.path.join(self.tmp, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"positions": [{"symbol": "SWIGGY"}]}, fh)
        code, out, err = run(["new", "--offline", self.off, "--state", self.state, "--now", NOW,
                              "--portfolio", pf, "--watchlist", os.path.join(self.tmp, "none.json"),
                              "--tickers", "tonbo", "--limit", "0"])
        self.assertEqual(code, 0, err)
        self.assertIn("SWIGGY is on your holdings", out)
        self.assertIn("TONBO is on your list", out)
        self.assertIn("On your lists: SWIGGY, TONBO.", out)

    def test_top_by_value(self):
        code, out, err = self.go("top", "--n", "3")
        self.assertEqual(code, 0, err)
        lines = [l for l in out.splitlines() if l.startswith("  2026")]
        self.assertEqual(len(lines), 3)
        self.assertIn("SWIGGY", lines[0])
        self.assertIn("SELL", lines[0])
        self.assertIn("265.34 crore", lines[0])

    def test_top_days_spans_stored_history(self):
        self.go("new")
        self.put(HEADER + "24-OCT-2026,AAA,A Ltd,BIG FUND,BUY,100000000,500.00,-\n", NO_BLOCK)
        code, out, err = self.go("top", "--days", "1", "--n", "1")
        self.assertIn("AAA", out)
        code, out, err = self.go("top", "--days", "2", "--n", "2")
        self.assertIn("AAA", out)
        self.assertIn("5000.00 crore", out)

    def test_who_summarises_history(self):
        code, out, err = self.go("who", "invesco")
        self.assertEqual(code, 0, err)
        self.assertIn("INVESCO MUTUAL FUND", out)
        self.assertIn("SMT", out)
        self.assertIn("net buyer", out)
        self.assertIn("History holds 1 day", out)

    def test_who_unknown_client(self):
        code, out, err = self.go("who", "no such client xyz")
        self.assertEqual(code, 1)
        self.assertIn("No deals by", out)

    def test_who_json(self):
        code, out, err = self.go("who", "GRT STRATEGIC", "--json")
        doc = json.loads(out)
        self.assertEqual(doc["clients"], ["GRT STRATEGIC VENTURES LLP"])
        self.assertIn("SMT", [x["symbol"] for x in doc["stocks"]])

    def test_dry_run_writes_nothing(self):
        self.go("new", "--dry-run")
        self.assertFalse(os.path.exists(self.state))

    def test_stale_file_is_called_out(self):
        code, out, err = self.go("show")
        self.assertIn("3 days ago", out)

    def test_html_error_page_is_refused(self):
        self.put("<html>Access Denied</html>", NO_BLOCK)
        code, out, err = self.go("new")
        self.assertEqual(code, 2)
        self.assertIn("does not look like NSE's deal CSV", err)

    def test_empty_day(self):
        self.put(NO_BLOCK, NO_BLOCK)
        code, out, err = self.go("new")
        self.assertEqual(code, 1)
        self.assertIn("NO RECORDS", out)

    def test_no_command_prints_help(self):
        self.assertEqual(run([])[0], 2)


if __name__ == "__main__":
    unittest.main()
