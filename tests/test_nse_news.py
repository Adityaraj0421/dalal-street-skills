import datetime
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("nse-news", "news")
DIR = os.path.join(FIX, "nse-news")
NOW = "2026-10-26 10:40"            # India time; the feeds were fetched at 10:35 IST on Mon Oct 26 2026
UTC = datetime.timezone.utc


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


def item(title, ticker="X", summary=""):
    return {"id": "gn:" + title, "source": "Test", "kind": "headline", "ticker": ticker, "title": title,
            "summary": summary, "link": "https://example.test/" + re.sub(r"\W+", "-", title), "time": None}


class Feeds(unittest.TestCase):
    def test_google_news_rss(self):
        items = news.parse_gnews(raw("gnews-RELIANCE.xml"), "RELIANCE")
        self.assertEqual(len(items), 26)
        by = {i["title"]: i for i in items}
        jio = by["Reliance Industries: Jio Platforms IPO Closes at ₹1,119 Per Share"]    # ' - InvestyWise' stripped
        self.assertEqual(jio["source"], "InvestyWise")
        self.assertEqual(jio["time"], datetime.datetime(2026, 10, 24, 19, 28, 47, tzinfo=UTC))
        self.assertTrue(jio["link"].startswith("https://news.google.com/rss/articles/"))
        self.assertTrue(jio["id"].startswith("gn:"))
        self.assertEqual(jio["kind"], "headline")
        # an item whose publisher is not in the title keeps its title
        self.assertTrue(all(not i["title"].endswith(" - " + i["source"]) for i in items))

    def test_entities_are_unescaped(self):
        items = news.parse_gnews(raw("gnews-TCS.xml"), "TCS")
        self.assertTrue(any("Why Are TCS, Infosys, Coforge & Other IT Stocks Rising Today?" in i["title"]
                            for i in items))
        self.assertFalse(any("&amp;" in i["title"] for i in items))

    def test_a_feed_that_is_not_xml(self):
        with self.assertRaises(news.NewsError):
            news.parse_gnews(b"<html>Sorry</html", "TCS")
        self.assertEqual(news.parse_gnews(b"<rss><channel></channel></rss>", "TCS"), [])

    def test_times(self):
        self.assertEqual(news.parse_time("Mon, 26 Oct 2026 03:30:25 GMT").hour, 3)
        self.assertEqual(news.parse_time("2026-10-26T09:00:00+05:30").hour, 3)
        self.assertEqual(news.to_ist(news.parse_time("Mon, 26 Oct 2026 03:30:25 GMT")),
                         datetime.datetime(2026, 10, 26, 9, 0, 25))
        self.assertIsNone(news.parse_time("soon"))

    def test_symbols(self):
        self.assertEqual(news.norm("reliance.ns"), "RELIANCE")
        self.assertEqual(news.norm("NSE:TCS"), "TCS")
        self.assertEqual(news.norm("m&m"), "M&M")
        self.assertEqual(news.norm("RELIANCE.BO"), "RELIANCE.BO")       # a BSE listing keeps its suffix
        with self.assertRaises(news.NewsError):
            news.norm("NIFTY")
        self.assertEqual(news.safe("M&M"), "M_M")
        self.assertEqual(news.safe("BAJAJ-AUTO"), "BAJAJ-AUTO")


class Names(unittest.TestCase):
    def test_company_names(self):
        self.assertEqual(news.clean_name("Reliance Industries Limited"), "Reliance Industries")
        self.assertEqual(news.clean_name("Info Edge (India) Ltd."), "Info Edge (India)")
        self.assertEqual(news.clean_name("Tata Motors Limited (DVR)"), "Tata Motors")
        self.assertEqual(news.clean_name("Tata Motors Ltd"), "Tata Motors")
        self.assertEqual(news.clean_name("Mahindra & Mahindra Limited"), "Mahindra & Mahindra")
        self.assertEqual(news.clean_name(None), "")
        names = news.parse_equity_l(raw("EQUITY_L.csv"))
        self.assertEqual(names["M&M"], "Mahindra & Mahindra Limited")       # the header has spaces after the commas
        self.assertEqual(names["RELIANCE"], "Reliance Industries Limited")
        with self.assertRaises(news.NewsError):
            news.parse_equity_l("<html>Access Denied</html>")

    def test_the_search_query(self):
        q = news.build_query("RELIANCE", "Reliance Industries Limited", 48)
        self.assertEqual(q, '"Reliance Industries" OR "RELIANCE share" when:48h')
        self.assertEqual(news.build_query("TCS", None, 6), '"TCS share" OR "TCS NSE" when:6h')
        self.assertEqual(news.build_query("TCS", "Tata Consultancy Services Limited", 24 * 30), '"Tata Consultancy Services" OR "TCS share" when:7d')
        url = news.GNEWS % news.urllib.parse.quote_plus(news.build_query("M&M", "Mahindra & Mahindra Limited", 48))
        self.assertIn("q=%22Mahindra+%26+Mahindra%22+OR+%22M%26M+share%22+when%3A48h&hl=en-IN&gl=IN&ceid=IN:en", url)
        self.assertTrue(url.startswith("https://news.google.com/rss/search?q="))

    def test_the_name_list_is_cached_for_a_week(self):
        tmp = tempfile.mkdtemp()
        try:
            asked = []

            class F(news.Fetcher):
                def get(self, url, name):
                    asked.append(url)
                    return raw("EQUITY_L.csv")
            f = F()
            names = news.equity_l(f, tmp, datetime.date(2026, 10, 26))
            self.assertEqual(names["TCS"], "Tata Consultancy Services Limited")
            self.assertTrue(os.path.exists(os.path.join(tmp, "EQUITY_L-2026-10-26.csv")))
            self.assertEqual(asked, ["https://archives.nseindia.com/content/equities/EQUITY_L.csv"])
            news.equity_l(f, tmp, datetime.date(2026, 11, 2))
            self.assertEqual(len(asked), 1)                               # 7 days old: reused
            news.equity_l(f, tmp, datetime.date(2026, 11, 3))
            self.assertEqual(len(asked), 2)                               # 8 days old: fetched again

            class Down(news.Fetcher):
                def get(self, url, name):
                    raise news.NewsError("HTTP 503 for " + url)
            names = news.equity_l(Down(), tmp, datetime.date(2026, 12, 25))  # falls back to the old file
            self.assertIn("RELIANCE", names)
            errors = []
            watch = {"TCS": None, "ZZZZ": None}
            news.fill_names(Down(), watch, os.path.join(tmp, "empty"), datetime.date(2026, 12, 25), errors)
            self.assertEqual(watch, {"TCS": None, "ZZZZ": None})
            self.assertIn("searching by ticker only", errors[0])
        finally:
            shutil.rmtree(tmp)

    def test_names_fill_in_from_the_list(self):
        errors = []
        watch = {"TCS": None, "RELIANCE": "Reliance Industries", "ZZZZ": None, "RELIANCE.BO": None}
        news.fill_names(news.Fetcher(offline=DIR), watch, "unused", datetime.date(2026, 10, 26), errors)
        self.assertEqual(watch, {"TCS": "Tata Consultancy Services Limited", "RELIANCE": "Reliance Industries",
                                 "ZZZZ": None, "RELIANCE.BO": "Reliance Industries Limited"})
        self.assertEqual(errors, [])


class Tagging(unittest.TestCase):
    CASES = [
        ("TCS Q2 results: net profit rises 8% to Rs 12,000 crore", "results"),
        ("Infosys raises FY27 revenue guidance after strong quarter", "results"),
        ("HDFC Bank declares interim dividend, record date fixed", "dividend"),
        ("Company announces 1:1 bonus shares", "bonus"),
        ("Board approves stock split in the ratio of 1:5", "split"),
        ("Infosys board approves Rs 18,000 crore share buyback", "buyback"),
        ("L&T bags orders worth Rs 5,000 crore", "order win"),
        ("BHEL wins contract from NTPC", "order win"),
        ("Tata Steel to acquire stake in overseas miner", "deal"),
        ("Block deal: promoter sells 2% in Zomato", "block/bulk deal"),
        ("Bulk deals: SBI MF buys Paytm shares", "block/bulk deal"),
        ("Jefferies upgrades Reliance, raises target price to Rs 1,600", "rating/target"),
        ("Brokerage cuts rating on Wipro", "rating/target"),
        ("SEBI issues show-cause notice to promoter", "SEBI/regulatory"),
        ("Ambuja Cements MD & CEO resigns", "management change"),
        ("Company appoints new chairman", "management change"),
        ("Hyundai India QIP opens, raises Rs 5,000 crore", "fund raise"),
        ("Jio Platforms IPO allotment today", "fund raise"),
        ("RBI keeps repo rate unchanged; banks rally", "macro/RBI"),
        ("FIIs sold Rs 4,000 crore of equities", "macro/RBI"),
        ("Stock hits upper circuit after results", "circuit"),
    ]

    def test_every_tag(self):
        for title, want in self.CASES:
            tags = news.tag([item(title)], {"X": None})[0]["tags"]
            self.assertIn(want, tags, title)
        wanted = {w for _, w in self.CASES}
        self.assertEqual(wanted, {l for l, _ in news.KEYWORDS})        # every tag has a case

    def test_quiet_headlines_get_no_tag(self):
        for title in ("Pithampur residents march for road safety", "Anuj Somani takes charge of marketing"[:20],
                      "Scholarships This Week"):
            self.assertEqual(news.tag([item(title)], {"X": None})[0]["tags"], [], title)

    def test_real_headlines(self):
        items = news.tag(news.parse_gnews(raw("gnews-RELIANCE.xml"), "RELIANCE"), {"RELIANCE": None})
        by = {i["title"]: i["tags"] for i in items}
        self.assertEqual(by["Reliance Industries: Jio Platforms IPO Closes at ₹1,119 Per Share"], ["fund raise"])
        items = news.tag(news.parse_gnews(raw("gnews-M_M.xml"), "M&M"), {"M&M": None})
        by = {i["title"]: i["tags"] for i in items}
        self.assertEqual(by["Mahindra Finance Q2 Results: Profit Rises To Rs 882 Cr, Revenue Up 15%"], ["results"])

    def test_touches_by_name_ticker_and_query(self):
        watch = {"RELIANCE": "Reliance Industries Limited", "HDFCBANK": "HDFC Bank Limited", "TCS": None,
                 "M&M": "Mahindra & Mahindra Limited"}
        a = news.tag([item("HDFC Bank, Bajaj Finance, Reliance Industries: market cap shrinks", ticker="TCS")], watch)[0]
        self.assertEqual(a["touches"], ["HDFCBANK", "RELIANCE", "TCS"])      # TCS only because the query was TCS
        b = news.tag([item("Did Reliance Industries Limited (NSE:RELIANCE) expand revenue?", ticker="RELIANCE")], watch)[0]
        self.assertEqual(b["touches"], ["RELIANCE"])
        c = news.tag([item("TCS and mahindra & mahindra lead Nifty gains", ticker="X")], watch)[0]
        self.assertEqual(c["touches"], ["M&M", "TCS"])                      # a ticker as written, a name in any case
        d = news.tag([item("Tcs is not a mention", ticker="X")], watch)[0]
        self.assertEqual(d["touches"], [])
        e = news.tag([item("M&M shares rise", ticker="X")], {"M&M": None})[0]
        self.assertEqual(e["touches"], ["M&M"])

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_gnews(raw("gnews-TCS.xml"), "TCS")
        b = news.parse_gnews(raw("gnews-TCS.xml"), "HDFCBANK")
        items = news.dedupe(news.tag(a + b, {"TCS": None, "HDFCBANK": None}))
        self.assertEqual(len(items), len(a))
        self.assertEqual(items[0]["touches"], ["HDFCBANK", "TCS"])
        # the same title on the same day from two publishers' links is one story
        x, y = item("Same story"), item("Same story")
        x["id"], y["id"], x["link"], y["link"] = "gn:1", "gn:2", "l1", "l2"
        x["time"] = y["time"] = datetime.datetime(2026, 10, 26, 3, 0, tzinfo=UTC)
        self.assertEqual(len(news.dedupe(news.tag([x, y], {"X": None}))), 1)


class Moves(unittest.TestCase):
    def test_a_real_chart(self):
        m = news.parse_chart(raw("chart-RELIANCE.json"), "RELIANCE")
        self.assertEqual((m["price"], m["prev_close"], m["pct"]), (1196.8, 1190.0, 0.57))
        self.assertEqual(news.to_ist(m["time"]).strftime("%H:%M"), "10:35")
        line = news.render_moves([m])[0]
        self.assertEqual(line, "RELIANCE     ₹1,196.80 +0.57% vs previous close ₹1,190.00 at 10:35 IST  ")
        quiet = dict(m, pct=1.2)
        self.assertNotIn("MOVING", news.render_moves([quiet])[0])
        self.assertIn("MOVING", news.render_moves([quiet], move_pct=1.0)[0])
        self.assertIn("MOVING", news.render_moves([dict(m, pct=-3.5)])[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")
        with self.assertRaises(news.NewsError):
            news.parse_chart(b"not json", "ZZZZ")

    def test_the_ampersand_in_m_and_m(self):
        m = news.parse_chart(raw("chart-M_M.json"), "M&M")
        self.assertEqual((m["price"], m["prev_close"]), (2861.8, 2861.2))

    def test_collect_moves_offline(self):
        errors = []
        moves = news.collect_moves(news.Fetcher(offline=DIR), {"TCS": None, "M&M": None, "NOFILE": None}, errors)
        self.assertEqual([m["ticker"] for m in moves], ["M&M", "TCS"])
        self.assertEqual(errors, [])                                       # a missing offline chart is silent

    def test_the_online_chart_uses_the_yahoo_helper(self):
        asked = []
        old = news.yahoo_get
        news.yahoo_get = lambda path: asked.append(path) or raw("chart-M_M.json").decode("utf-8")
        try:
            f = news.Fetcher(pause=0)
            moves = news.collect_moves(f, {"M&M": None, "RELIANCE.BO": None}, [])
        finally:
            news.yahoo_get = old
        self.assertEqual(asked, ["/v8/finance/chart/M%26M.NS?range=1d&interval=5m",
                                 "/v8/finance/chart/RELIANCE.BO?range=1d&interval=5m"])
        self.assertEqual(len(moves), 2)


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        none = os.path.join(self.tmp.name, "none.json")
        # --loose: these tests predate the loose-match filter and check other things; Filtering tests it
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", none, "--watchlist", none, "--plan", none, "--loose"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "RELIANCE", "TCS", "M&M", "--hours", "48", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(data["checked"], "2026-10-26T05:10:00+00:00")
        self.assertEqual(sorted(m["ticker"] for m in data["moves"]), ["M&M", "RELIANCE", "TCS"])
        self.assertIn("nseindia.com > Corporate Announcements", data["not_covered"])
        self.assertTrue(all(i["new"] for i in data["items"]))
        self.assertTrue(all(datetime.datetime.fromisoformat(t) >= datetime.datetime(2026, 10, 24, 5, 10, tzinfo=UTC)
                            for t in times))
        # run it again: nothing new
        code, out, _ = run(["--tickers", "RELIANCE", "TCS", "M&M", "--hours", "48"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)
        code, out, _ = run(["--tickers", "RELIANCE", "TCS", "M&M", "--hours", "48", "--all", "--json", "--dry-run"]
                           + self.base)
        self.assertEqual(code, 1)                              # items are shown, but none is new
        self.assertEqual([i["new"] for i in json.loads(out)["items"]], [False] * len(json.loads(out)["items"]))

    def test_the_ist_text(self):
        code, out, err = run(["--tickers", "TCS", "M&M", "--hours", "6", "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "M&M          ₹2,861.80 +0.02% vs previous close ₹2,861.20 at 10:35 IST  ")
        self.assertTrue(lines[1].startswith("TCS"))
        # the window starts 04:40 IST on the 26th; times are IST, newest first
        stamps = [l.split("  ")[1] for l in lines if l.startswith("  2026-")]
        self.assertEqual(stamps, ["2026-10-26 09:37 IST", "2026-10-26 09:00 IST", "2026-10-26 05:44 IST",
                                  "2026-10-26 05:14 IST"])
        self.assertIn("Indian IT companies see business in policing AI agents", out)
        self.assertIn("Checked 2026-10-26 10:40 IST. Headlines are leads, not facts", out)
        self.assertIn("Not financial advice.", out)
        self.assertIn("nseindia.com > Corporate Announcements", out)
        self.assertNotIn(chr(0x2014), out)                     # no em dashes
        self.assertNotIn(" ET", out)

    def test_nothing_from_after_the_pinned_clock(self):
        args = [a if a != NOW else "2026-10-26 09:10" for a in self.base]
        code, out, err = run(["--tickers", "TCS", "M&M", "--hours", "6", "--no-moves", "--dry-run"] + args)
        self.assertNotIn("09:37 IST", out)                     # published after the pinned 09:10 clock
        self.assertIn("2026-10-26 09:00 IST", out)
        self.assertNotIn("vs previous close", out)

    def test_tags_and_touches_in_the_output(self):
        code, out, err = run(["--tickers", "M&M", "--hours", "72", "--no-moves", "--dry-run", "--links"] + self.base)
        lines = out.splitlines()
        i = [n for n, l in enumerate(lines) if "Mahindra Finance Q2 Results" in l][0]
        self.assertIn("2026-10-25 13:15 IST  M&M", lines[i])
        self.assertIn("tags: results", lines[i + 1])
        self.assertIn("https://news.google.com/rss/articles/", lines[i + 1])

    def test_per_ticker_cap(self):
        code, out, _ = run(["--tickers", "TCS", "--hours", "2400", "--per-ticker", "5", "--json", "--no-moves",
                            "--dry-run"] + self.base)
        self.assertEqual(len(json.loads(out)["items"]), 5)
        code, out, _ = run(["--tickers", "TCS", "--hours", "2400", "--per-ticker", "0", "--json", "--no-moves",
                            "--dry-run"] + self.base)
        self.assertGreater(len(json.loads(out)["items"]), 20)

    def test_tickers_from_watchlist_plan_and_portfolio(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "nse-scanner",
                       "hits": [{"symbol": "TCS", "setups": ["orb"], "name": "Tata Consultancy Services Ltd."},
                                {"symbol": "M&M", "setups": ["delivery"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "nse-exit", "symbol": "RELIANCE"}, fh)
        pf = os.path.join(self.tmp.name, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"positions": [{"symbol": "hdfcbank"}], "holdings": [{"tradingsymbol": "INFY"}]}, fh)
        w = news.watch_from_files(pf, wl, plan)
        self.assertEqual(w, {"TCS": "Tata Consultancy Services Ltd.", "M&M": None, "RELIANCE": None,
                             "HDFCBANK": None, "INFY": None})
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, "/nonexistent.json")), ["M&M", "TCS"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run", "--hours", "24"] + args)
        self.assertEqual(code, 0, err)
        tickers = {t for i in json.loads(out)["items"] for t in i["touches"]}
        self.assertEqual(tickers, {"TCS", "M&M"})
        self.assertFalse(os.path.exists(self.state))

    def test_no_tickers_and_bad_tickers(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no tickers", err)
        self.assertIn("nse/watchlist.json", err)
        code, _, err = run(["--tickers", "NIFTY"] + self.base)
        self.assertEqual(code, 2)
        self.assertIn("is an index", err)
        args = [a if a != NOW else "tomorrow" for a in self.base]
        code, _, err = run(["--tickers", "TCS"] + args)
        self.assertEqual(code, 2)
        self.assertIn("--now wants an India time", err)

    def test_a_missing_feed_is_a_note_not_a_crash(self):
        code, out, err = run(["--tickers", "TCS", "ZZZZ", "--hours", "6", "--no-moves", "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("note: headlines for ZZZZ: offline: no file", out)
        self.assertNotIn("Traceback", err)

    def test_the_search_url_is_what_google_expects(self):
        asked = []

        class F(news.Fetcher):
            def get(self, url, name):
                asked.append((url, name))
                return raw("gnews-M_M.xml") if name.startswith("gnews") else raw("EQUITY_L.csv")
        old = news.Fetcher
        news.Fetcher = lambda offline=None: F(offline=None, pause=0)
        try:
            code, out, err = run(["--tickers", "M&M", "--hours", "48", "--no-moves", "--dry-run", "--state", self.state,
                                  "--now", NOW, "--portfolio", "x", "--watchlist", "x", "--plan", "x"])
        finally:
            news.Fetcher = old
        urls = [u for u, _ in asked]
        self.assertEqual(asked[0][0], "https://archives.nseindia.com/content/equities/EQUITY_L.csv")
        self.assertEqual(urls[1], "https://news.google.com/rss/search?q=%22Mahindra+%26+Mahindra%22+OR+%22M%26M+share%22"
                                  "+when%3A48h&hl=en-IN&gl=IN&ceid=IN:en")
        self.assertEqual(asked[1][1], "gnews-M_M.xml")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "state", "EQUITY_L-2026-10-26.csv")))
        self.assertEqual(code, 0, err)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-08-01T00:00:00+00:00"}}
        now = news.parse_time("2026-10-26T04:40:00+00:00")
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())

    def test_the_window_and_the_future(self):
        now = news.parse_time("2026-10-26T04:40:00+00:00")
        old = item("old story")
        old["time"] = now - datetime.timedelta(hours=60)
        future = item("future story")
        future["time"] = now + datetime.timedelta(hours=1)
        fresh = item("fresh story")
        fresh["time"] = now - datetime.timedelta(hours=1)
        undated = item("undated story")
        shown = news.select([old, future, fresh, undated], {}, now, hours=48)
        self.assertEqual([i["title"] for i in shown], ["undated story", "fresh story"])


class Guards(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nse-news", "news.py"),
                  encoding="utf-8") as fh:
            self.src = fh.read()

    def test_no_us_leftovers_and_no_protected_endpoints(self):
        for bad in ("NYSE", "New York", "Eastern", "sec.gov", "EDGAR", "8-K", "Alpaca", "trader/",
                    "feeds.finance.yahoo.com", "www.nseindia.com", "api.bseindia.com"):
            self.assertNotIn(bad, self.src, bad)
        self.assertIsNone(re.search(r"(?<!as )\bET\b(?!\.)", self.src))     # ET.fromstring is ElementTree
        self.assertIsNone(re.search(r"\$\s?[\d{%]", self.src))

    def test_core_section_is_the_shared_code(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(root, "_shared", "india_core.py"), encoding="utf-8") as fh:
            core = fh.read().split("\n")
        end = core.index("    return inr(x, 0)") + 1          # console .. money: what news pastes
        self.assertIn("\n".join(core[11:end]), self.src)


class Filtering(unittest.TestCase):
    """Positional tickers, loosely matched headlines hidden, links on request, tighter tags."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        none = os.path.join(self.tmp.name, "none.json")
        self.base = ["--offline", DIR, "--state", os.path.join(self.tmp.name, "news.json"), "--now", NOW,
                     "--portfolio", none, "--watchlist", none, "--plan", none, "--no-moves", "--dry-run",
                     "--hours", "2400", "--per-ticker", "0"]

    def tearDown(self):
        self.tmp.cleanup()

    def titles(self, extra):
        code, out, _ = run(extra + ["--json"] + self.base)
        return [i["title"] for i in json.loads(out)["items"]]

    def test_positional_tickers_work_like_tickers(self):
        self.assertEqual(self.titles(["TCS"]), self.titles(["--tickers", "TCS"]))

    def test_headlines_that_never_name_the_stock_are_hidden(self):
        strict, loose = self.titles(["TCS"]), self.titles(["TCS", "--loose"])
        self.assertLess(len(strict), len(loose))
        self.assertNotIn("Indian IT companies see business in policing AI agents", strict)
        self.assertIn("Indian IT companies see business in policing AI agents", loose)
        for t in strict:
            self.assertRegex(t, r"TCS|Tata Consultancy Services")

    def test_the_hidden_count_and_link_hint_are_said(self):
        code, out, _ = run(["TCS"] + self.base)
        self.assertRegex(out, r"\(\d+ loosely matched headlines? hidden")
        self.assertNotIn("news.google.com", out)
        self.assertIn("--links prints them", out)
        code, out, _ = run(["TCS", "--links"] + self.base)
        self.assertIn("news.google.com", out)

    def test_a_ceo_talking_is_not_a_management_change(self):
        def tags(title):
            it = [{"title": title, "summary": "", "ticker": "HCLTECH"}]
            return news.tag(it, {"HCLTECH": "HCL Technologies Limited"})[0]["tags"]
        self.assertNotIn("management change", tags("AI marks inflection point, future belongs to those who reinvent: HCLTech CEO"))
        self.assertIn("management change", tags("HCLTech appoints new CFO"))
        self.assertIn("management change", tags("HCLTech CEO steps down"))
        self.assertIn("management change", tags("HCLTech names C Vijayakumar as chairman"))
        self.assertIn("rating/target", tags("HCLTech launches NVIDIA tool: LKP sets INR 1,360 target for HCL Tech stock"))
        it = news.tag([{"title": "HCLTech CEO on AI", "summary": "", "ticker": "HCLTECH"}],
                      {"HCLTECH": "HCL Technologies Limited"})[0]
        self.assertEqual(it["mentioned"], ["HCLTECH"])   # long tickers match in any case


if __name__ == "__main__":
    unittest.main()
