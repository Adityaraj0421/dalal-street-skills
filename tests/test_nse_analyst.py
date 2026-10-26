import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

facts = load("nse-analyst", "facts")
DIR = os.path.join(FIX, "nse-analyst")
NOW = "2026-10-26 10:00"


def data(sym):
    with open(os.path.join(DIR, sym + ".NS.json"), encoding="utf-8") as fh:
        return facts.parse_timeseries(json.load(fh))


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = facts.main(args)
    return code, out.getvalue(), err.getvalue()


def synth(rev_by_date, ni_by_date=None):
    """A minimal Yahoo-shaped parse: {type: {date: value}}."""
    d = {"quarterlyTotalRevenue": dict(rev_by_date)}
    if ni_by_date:
        d["quarterlyNetIncome"] = dict(ni_by_date)
    return d


class FiscalCalendar(unittest.TestCase):
    def test_labels(self):
        lab = lambda s: facts.qlabel(facts.qindex(s))
        self.assertEqual(lab("2026-06-30"), "Q1FY27")
        self.assertEqual(lab("2026-09-30"), "Q2FY27")
        self.assertEqual(lab("2026-12-31"), "Q3FY27")
        self.assertEqual(lab("2027-03-31"), "Q4FY27")
        self.assertEqual(lab("2026-03-31"), "Q4FY26")

    def test_not_a_quarter_end(self):
        self.assertIsNone(facts.qindex("2026-05-15"))

    def test_qend_roundtrip(self):
        for d in ("2026-06-30", "2026-09-30", "2026-12-31", "2027-03-31"):
            self.assertEqual(facts.qend(facts.qindex(d)).isoformat(), d)

    def test_deadline_45_days_and_60_for_march(self):
        self.assertEqual(facts.report_deadline(facts.qindex("2026-09-30")).isoformat(), "2026-11-14")
        self.assertEqual(facts.report_deadline(facts.qindex("2026-03-31")).isoformat(), "2026-05-30")


class Parsing(unittest.TestCase):
    def test_keys_that_work_for_indian_stocks(self):
        d = data("RELIANCE")
        for k in ("quarterlyTotalRevenue", "quarterlyNetIncome", "quarterlyDilutedEPS",
                  "quarterlyEBITDA", "quarterlyOperatingIncome", "annualTotalRevenue",
                  "annualFreeCashFlow", "annualTotalDebt"):
            self.assertIn(k, d)
        self.assertEqual(d["quarterlyTotalRevenue"]["2026-06-30"], 3094680000000.0)

    def test_unknown_symbol_is_an_error(self):
        code, out, err = run(["ZZZZNOPE", "--offline", DIR, "--now", NOW])
        self.assertEqual(code, 2)
        self.assertIn("no results data", err)

    def test_yahoo_error_body(self):
        with self.assertRaises(facts.IndiaError):
            facts.parse_timeseries({"timeseries": {"result": None, "error": {"code": "Bad", "description": "boom"}}})


class Reliance(unittest.TestCase):
    def setUp(self):
        self.r = facts.analyse(data("RELIANCE"), quarters=7, now_date=datetime.date(2026, 10, 26))

    def test_latest_quarter(self):
        q = self.r["rows"][0]
        self.assertEqual(q["quarter"], "Q1FY27")
        self.assertEqual(q["end"], "2026-06-30")
        self.assertEqual(q["revenue"], 3094680000000.0)
        self.assertEqual(q["net_income"], 209460000000.0)
        self.assertEqual(q["eps"], 15.48)
        self.assertEqual(q["revenue_yoy"], 27.0)     # vs 2,43,632 crore in Q1FY26
        self.assertEqual(q["net_income_yoy"], -22.4)  # vs 26,994 crore
        self.assertEqual(q["operating_margin"], 10.5)
        self.assertEqual(q["net_margin"], 6.8)

    def test_gap_is_shown_not_filled(self):
        rows = {r["quarter"]: r for r in self.r["rows"]}
        self.assertTrue(rows["Q2FY26"]["missing"])
        self.assertNotIn("revenue", rows["Q2FY26"])
        self.assertIn("Q2FY26", self.r["gaps"])

    def test_no_yoy_when_year_ago_quarter_absent(self):
        rows = {r["quarter"]: r for r in self.r["rows"]}
        # Q3FY26's year-ago quarter (Q3FY25, 2024-12-31) is not on Yahoo
        self.assertIsNone(rows["Q3FY26"]["revenue_yoy"])
        self.assertFalse(rows["Q3FY26"]["has_year_ago"])

    def test_no_ttm_across_a_gap(self):
        self.assertNotIn("revenue", self.r["ttm"])
        self.assertTrue(any("Q2FY26" in n for n in self.r["ttm_notes"]))

    def test_annual_and_cash_flow(self):
        a = {x["year"]: x for x in self.r["annual"]}
        self.assertEqual(a["FY26"]["revenue"], 10572190000000.0)
        self.assertEqual(a["FY26"]["revenue_yoy"], 9.6)
        self.assertEqual(a["FY26"]["free_cash_flow"], 691970000000.0)
        self.assertEqual(a["FY26"]["eps"], 59.69)

    def test_gross_from_cost_when_gross_absent(self):
        d = data("RELIANCE")
        d.pop("quarterlyGrossProfit")
        r = facts.analyse(d)
        q = r["rows"][0]
        self.assertTrue(q["gross_from_cost"])
        self.assertEqual(q["gross_profit"], 3094680000000.0 - 2264640000000.0)


class Tcs(unittest.TestCase):
    def setUp(self):
        self.r = facts.analyse(data("TCS"), quarters=6, now_date=datetime.date(2026, 10, 26))

    def test_ttm_with_four_consecutive_quarters(self):
        # Q2FY27 + Q1FY27 + Q4FY26 + Q3FY26
        self.assertEqual(self.r["ttm"]["revenue_to"], "Q2FY27")
        self.assertAlmostEqual(self.r["ttm"]["revenue"] / 1e7, 283248, delta=1)
        self.assertEqual(self.r["ttm"]["eps"], 142.66)

    def test_yoy_only_where_the_year_ago_quarter_exists(self):
        rows = {r["quarter"]: r for r in self.r["rows"]}
        self.assertIsNone(rows["Q2FY27"]["revenue_yoy"])    # Q2FY26 missing
        self.assertEqual(rows["Q1FY27"]["revenue_yoy"], 13.9)

    def test_what_changed_says_when_growth_is_missing(self):
        lines = facts.changes(self.r)
        self.assertIn("Q2FY27", lines[0])
        self.assertTrue(any("not on Yahoo, so there is no year-on-year" in l for l in lines))

    def test_next_results_due(self):
        self.assertEqual(facts.next_due(self.r), ("Q3FY27", "2027-02-14"))


class Synthetic(unittest.TestCase):
    def test_four_consecutive_quarters_make_a_ttm(self):
        d = synth({"2026-06-30": 40e9, "2026-03-31": 30e9, "2025-12-31": 20e9, "2025-09-30": 10e9})
        r = facts.analyse(d)
        self.assertEqual(r["ttm"]["revenue"], 100e9)

    def test_three_quarters_no_ttm(self):
        d = synth({"2026-06-30": 40e9, "2026-03-31": 30e9, "2025-12-31": 20e9})
        self.assertNotIn("revenue", facts.analyse(d)["ttm"])

    def test_yoy_math(self):
        d = synth({"2026-06-30": 120.0, "2025-06-30": 100.0})
        r = facts.analyse(d, quarters=1)
        self.assertEqual(r["rows"][0]["revenue_yoy"], 20.0)

    def test_stale_note_when_results_overdue(self):
        d = synth({"2026-03-31": 1e9})
        r = facts.analyse(d, now_date=datetime.date(2026, 10, 26))
        self.assertIn("Q1FY27", r["stale"])    # due 2026-08-14
        r2 = facts.analyse(synth({"2026-06-30": 1e9}), now_date=datetime.date(2026, 10, 26))
        self.assertIsNone(r2["stale"])         # Q2FY27 not due until 2026-11-14

    def test_net_above_operating_is_flagged(self):
        d = synth({"2026-06-30": 100e9})
        d["quarterlyNetIncome"] = {"2026-06-30": 30e9}
        d["quarterlyOperatingIncome"] = {"2026-06-30": 20e9}
        lines = facts.changes(facts.analyse(d))
        self.assertTrue(any("above operating income" in l for l in lines))

    def test_no_revenue_or_profit_is_an_error(self):
        with self.assertRaises(facts.IndiaError):
            facts.analyse({"annualTotalRevenue": {"2026-03-31": 1.0}})


class Currency(unittest.TestCase):
    """Infosys reports in US dollars: never put a rupee sign on those numbers."""

    def test_currency_read_from_every_point(self):
        d = data("INFY")
        self.assertEqual(facts.reporting_currency(d), "USD")
        self.assertEqual(facts.foreign_currencies(d), ["USD"])
        self.assertEqual(facts.foreign_currencies(data("TCS")), [])

    def test_indian_names_report_in_rupees(self):
        for sym in ("WIPRO", "HDFCBANK", "ICICIBANK", "DRREDDY", "TCS", "RELIANCE"):
            self.assertEqual(facts.reporting_currency(data(sym)), "INR", sym)

    def test_default_is_native_currency_with_no_rupee_sign(self):
        code, out, err = run(["INFY", "--offline", DIR, "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertIn("Amounts in USD million", out)
        self.assertIn("reports in USD, not rupees", out)
        self.assertNotIn(facts.RUPEE, out)
        self.assertNotIn("crore", out.split("Official numbers")[0])
        self.assertIn("5,082", out)
        self.assertIn("EPS USD 0.20", out)

    def test_native_analysis_numbers(self):
        r = facts.analyse(data("INFY"))
        self.assertEqual(r["currency"], "USD")
        q = r["rows"][0]
        self.assertEqual(q["revenue"], 5082000000.0)
        self.assertEqual(q["revenue_yoy"], 2.9)       # vs 4,941 million
        self.assertEqual(r["fx_used"], {})

    def test_inr_converts_at_quarter_end_rates(self):
        code, out, err = run(["INFY", "--inr", "--offline", DIR, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        res = json.loads(out)[0]["result"]
        self.assertEqual(res["currency"], "INR")
        q = res["rows"][0]
        rate = res["fx_used"]["USD"]["2026-06-30"]
        self.assertEqual(rate, [94.918, "2026-06-30"])
        self.assertAlmostEqual(q["revenue"], 5082000000.0 * 94.918, delta=1)
        self.assertEqual(res["rows"][4]["quarter"], "Q1FY26")
        self.assertEqual(res["fx_used"]["USD"]["2025-06-30"][0], 85.714)   # that quarter's own rate
        # growth in rupees includes the dollar's rise, so it differs from the +2.9% in dollars
        self.assertEqual(q["revenue_yoy"], round(100 * (5082 * 94.918 / (4941 * 85.714) - 1), 1))

    def test_inr_text_output_says_what_it_did(self):
        code, out, err = run(["INFY", "--inr", "--offline", DIR, "--now", NOW])
        self.assertIn("Amounts in %s crore" % facts.RUPEE, out)
        self.assertIn("Converted from USD", out)
        self.assertIn("48,237", out)
        self.assertIn("currency moves", out)

    def test_inr_flag_leaves_rupee_reporters_alone(self):
        code, out, err = run(["TCS", "--inr", "--offline", DIR, "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Converted from", out)

    def test_rate_on_uses_last_close_on_or_before(self):
        s = [("2026-06-26", 1.0), ("2026-06-29", 2.0)]
        self.assertEqual(facts.rate_on(s, "2026-06-28"), (1.0, "2026-06-26"))
        self.assertIsNone(facts.rate_on(s, "2026-06-01"))

    def test_mixed_currencies_in_one_stock_are_never_blended(self):
        d = {"quarterlyTotalRevenue": {"2026-06-30": 100e9, "2026-03-31": 5e6},
             "quarterlyNetIncome": {"2026-06-30": 10e9},
             facts.CCY_KEY: {"quarterlyTotalRevenue": {"2026-06-30": "INR", "2026-03-31": "USD"},
                             "quarterlyNetIncome": {"2026-06-30": "INR"}}}
        r = facts.analyse(d, quarters=2)
        self.assertEqual(r["currency"], "INR")
        self.assertTrue(r["rows"][1]["missing"])      # the dollar point is a gap, not a number
        self.assertTrue(any("another currency" in n for n in r["currency_notes"]))
        # with rates, each point converts at its own date's rate
        out, used, notes = facts.convert_to_inr(d, {"USD": [("2026-03-30", 90.0)]})
        self.assertEqual(out["quarterlyTotalRevenue"]["2026-03-31"], 5e6 * 90.0)
        self.assertEqual(out["quarterlyTotalRevenue"]["2026-06-30"], 100e9)

    def test_missing_rate_leaves_the_point_out_with_a_note(self):
        d = {"quarterlyTotalRevenue": {"2026-06-30": 5e6},
             facts.CCY_KEY: {"quarterlyTotalRevenue": {"2026-06-30": "USD"}}}
        out, used, notes = facts.convert_to_inr(d, {"USD": [("2026-07-30", 90.0)]})
        self.assertNotIn("quarterlyTotalRevenue", out)
        self.assertTrue(notes)

    def test_delisted_symbol_has_no_data(self):
        code, out, err = run(["TATAMOTORS", "--offline", DIR])
        self.assertEqual(code, 2)


class Cli(unittest.TestCase):
    def test_table_in_crore(self):
        code, out, err = run(["RELIANCE", "TCS", "--offline", DIR, "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertIn("3,09,468", out)               # Indian digit grouping, crore
        self.assertIn("Q1FY27", out)
        self.assertIn("not on Yahoo (a gap, not a zero)", out)
        self.assertIn("nseindia.com", out)
        self.assertIn("Last 4 quarters (to Q2FY27): revenue 2,83,248 crore", out)

    def test_json(self):
        code, out, err = run(["TCS.NS", "--offline", DIR, "--json", "--now", NOW])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc[0]["ticker"], "TCS")
        self.assertEqual(doc[0]["result"]["latest_quarter"], "Q2FY27")

    def test_index_refused(self):
        code, out, err = run(["NIFTY", "--offline", DIR])
        self.assertEqual(code, 2)
        self.assertIn("index", err)

    def test_partial_failure_exit_code(self):
        code, out, err = run(["TCS", "ZZZZNOPE", "--offline", DIR, "--now", NOW])
        self.assertEqual(code, 1)

    def test_metrics_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nse", "metrics.json")
            self.assertEqual(run(["TCS", "--offline", DIR, "--metrics", path, "--now", NOW])[0], 0)
            self.assertEqual(run(["RELIANCE", "--offline", DIR, "--metrics", path, "--now", NOW])[0], 0)
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            rows = {r["ticker"]: r for r in doc["companies"]}
            self.assertEqual(set(rows), {"RELIANCE", "TCS"})
            self.assertEqual(rows["TCS"]["currency"], "INR")
            self.assertEqual(rows["TCS"]["eps_ttm"], 142.66)
            self.assertIsNone(rows["RELIANCE"]["revenue_ttm"])    # gap: no TTM

    def test_no_tickers_prints_help(self):
        code, out, err = run([])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
