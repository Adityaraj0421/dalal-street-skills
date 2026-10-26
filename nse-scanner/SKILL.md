---
name: nse-scanner
description: >-
  Chart Scanner for NSE/BSE. Scans hundreds of Indian charts for setups:
  opening-range breakouts, flags, oversold bounces and, for investors,
  delivery-percentage spikes. Builds its stock list from the Nifty indices
  (Nifty 50, 100, 200, 500, Midcap 150, Smallcap 250, Bank Nifty) on
  niftyindices.com or from every EQ-series stock on NSE, runs a scan of daily
  closes (20-day closing-high breakout, bull flag, reclaimed the 20-day
  average, oversold, oversold bounce), an intraday opening-range breakout scan
  on 5-minute bars from 09:15 IST, and a delivery scan on the NSE bhavcopy
  (delivery % and delivery quantity well above a stock's own average), then
  writes nse/watchlist.json for nse-news and nse-entry. Free Yahoo and NSE
  archive data. Use when the user says "scan the market", "find me setups",
  "what is breaking out", "opening range breakout", "Nifty 500 scan",
  "scan for bull flags", "oversold stocks", "delivery spike", "high delivery
  percentage", "accumulation", "stocks to invest in", "pre-market scan",
  "build my watchlist", "what should I look at today", for intraday (MIS) or
  delivery (CNC) ideas on RELIANCE, TCS, HDFCBANK and friends, or at the
  start of a trading day before /nse-news.
argument-hint: "[universe | daily | orb | delivery] [--universe nifty500] [--at HH:MM]"
---

# nse-scanner

On Windows, use `py` in place of `python3`.

The Chart Scanner is the first step of a trade, intraday or delivery. It
reads hundreds of free charts and files and lists the ones that match a rule.
That is all it does. A scan is a list of charts that match a rule, not a list
of stocks to buy, and a setup on a chart is not a prediction. Most people who
day trade lose money; the scanner does not change that. It only saves you
from flipping through 500 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                          # refresh the Nifty 500 list (cached in nse/state/)
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                             # daily-close setups -> nse/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --universe nifty50          # nifty50 nifty100 nifty200 nifty500 midcap150 smallcap250 bank all
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,TCS,M&M  # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                               # after 09:35 IST -> nse/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                    # cut the clock at 10:15 IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" delivery                          # delivery % and quantity spikes (bhavcopy)
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist nse/watchlist.json --json
```

Flags: `--universe` (default `nifty500`; `all` = every NSE EQ-series stock, about
2,300), `--min-price 10` (daily and orb; delivery 0), `--min-turnover-lakh`
(optional liquidity floor in rupees lakh, from the last bhavcopy; default off),
`--or-minutes 15`, `--max-confirm 200` (orb stage 2), `--now "2026-10-23 10:15"`
(India time) to pin the clock, `--offline PATH` to read saved Yahoo chart files,
`--lists DIR` to read the stock lists and bhavcopies from a folder, `--no-write`,
`--show N`, `--json`. Exit code 0 when the scan found something, 1 when it found
nothing or could not run (the reason is one plain line).

Delivery flags: `--history 7` (earlier bhavcopies to average), `--per-rise 10`
(delivery % points above the stock's own average), `--qty-mult 2.0` (delivery
quantity times its own average), `--min-delivery-pct 40`, `--min-history 3`.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/state/ind_nifty500list-YYYY-MM-DD.csv` (or `EQUITY_L-YYYY-MM-DD.csv`) | the stock list, cached with its date and reused for 7 days (`--refresh-lists` forces a download) |
| `nse/state/bhav/sec_bhavdata_full_DDMMYYYY.csv` | bhavcopies for the delivery scan and the turnover ranking, the newest 20 kept |
| `nse/watchlist.json` | written by `daily`, `orb` and `delivery`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. nse-news and nse-entry read `hits[].symbol`. Each hit carries `symbol`, `exchange`, `yahoo`, `series` (null when unknown), `name`, `setups`, `price`, plus the scan's own numbers |
| `nse/config.json` | read by `orb`: `no_entries_after` (default 14:45) |
| `nse/holidays.json` | optional calendar overrides (next year's NSE holidays, Muhurat timings) |

## The universe

`universe` downloads the constituent list of a Nifty index from
niftyindices.com (`ind_nifty50list.csv`, `ind_nifty100list`, `ind_nifty200list`,
`ind_nifty500list`, `ind_niftymidcap150list`, `ind_niftysmallcap250list`,
`ind_niftybanklist`), or `EQUITY_L.csv` from archives.nseindia.com for
`--universe all`, where only the `EQ` series is kept. BE and BZ (trade-for-trade,
delivery only) and SM / ST (SME) stocks are left out of `all`: they cannot be
traded intraday the normal way. Columns in NSE's files have spaces after the
commas, which are stripped. Symbols go through the shared symbol rules (`M&M`
and `BAJAJ-AUTO` are fine, escaped in the Yahoo URL). A BSE-only stock is
`RELIANCE.BO` style; BSE numeric codes do not work on Yahoo and are refused.

## The daily scan (after the close or before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request, so the
Nifty 500 takes 25 requests with a polite 0.3 s pause. Spark has no volume, so
liquidity comes from the NSE bhavcopy of the last trading day: the last close
must be at least `--min-price` and, if you ask for it, the turnover at least
`--min-turnover-lakh`. Hits are listed most traded first (turnover). Today's
daily bar is left out until 15:30 IST.

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 09:15,
09:20 and 09:25 five-minute bars. A hit is the first completed 5-minute bar to
CLOSE above the range high, at that bar's close time (IST).

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a request.
   A chart goes on only if a completed bar after the range closed at or above
   the highest close of the range (a necessary condition, so no breakout is
   lost). Yahoo's spark leaves some 5-minute bars out that the full chart has
   (a third of the Nifty 50 by 14:00 on 2026-10-23), so a chart with a gap is
   not thrown away: it goes to stage 2, and the scan says how many did.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one request
   each. Range high = the highest high of the range bars. Stage 2 checks at
   most `--max-confirm` (200) candidates, the most traded first (turnover of
   the last bhavcopy), and prints PARTIAL with the count it skipped.
   `--max-confirm 0` checks them all.

A chart is usable at stage 2 only when every 5-minute bar from the open to the
cutoff is there; a missing bar means a halt or a thin stock. Null closes are
dropped, a bar off the 5-minute grid (the live tick) is dropped, and a bar
counts only once it is complete. Before 09:35 IST, on a weekend or an NSE
holiday, after the close, or after `no_entries_after` (an expired setup) it
prints one plain line and writes nothing. On a special session such as Muhurat
trading (built in for 2026: Sunday 2026-11-08, 14:20 to 15:30; add other years
to `nse/holidays.json`) entries run from open + 15 minutes to close - 15
minutes. Yahoo keeps only a few days of 5-minute history, so replays older
than a week need `--offline`.

**Closing auction.** From 2026-08-03 to 2026-12-06, F&O stocks stop continuous
trading at 15:15 (the auction that follows takes no stop-loss orders), so
their chart is complete with no bar after 15:10; other stocks run to 15:30.
From 2026-12-07 every stock trades to 15:30 again. The orb scan reads each
stock's bars to its own continuous close and prints one line saying so. The
F&O list is NSE's open `fo_mktlots.csv`, cached in
`nse/state/fo_mktlots-YYYY-MM-DD.csv` (a cache up to 7 days old is reused if
the fetch fails, with a note; if nothing is available every stock is treated
as F&O, the cautious end). `--fo-lots FILE` gives the list by hand; `--offline`
uses the test copy. The scanner computes no charges, so the broker setting in
`nse/config.json` does not matter here.

## The delivery scan (for investors)

The NSE bhavcopy of each trading day (`sec_bhavdata_full_DDMMYYYY.csv`, open on
archives.nseindia.com) lists, for every stock, the quantity that was actually
delivered (taken home, not squared off the same day) and the delivery %. The
scan finds the latest trading day by walking back over the NSE calendar (the
file for a day appears after the close; a missing file is skipped), then reads
up to `--history` (7) earlier bhavcopies, caching each in `nse/state/bhav/`.
A stock is listed when, against its OWN average over those days:

- its delivery % is at least the average + `--per-rise` (10) points and at least
  `--min-delivery-pct` (40) %, and
- its delivery quantity is at least `--qty-mult` (2.0) times its average.

It needs `--min-history` (3) earlier days of data. Hits show the close, the
change on the day, delivery % against its average, quantity against its average
and the rupee value delivered (lakh / crore), largest value first. High
delivery is an accumulation signal investors use, not a prediction: a stock can
be heavily delivered on its way down, so the day's change and the news
(`/nse-news TICKER`) come next. BE-series stocks show no delivery data and are
skipped. On many days nothing passes the defaults; loosen one number at a time
and say which.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars are
  5-minute bars, never tick by tick. query2 first, query1 on a 429 or 5xx, one
  retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 480 of 500 charts (20 failed or
  had no data)". Never round a partial scan up to "the whole market".
- Only open files are used (niftyindices.com, archives.nseindia.com). NSE's
  website API (the nseindia.com/api pages) is behind bot protection and is never
  touched.
- The NSE holiday list covers 2026. NSE publishes the next year in December;
  until this skill is updated, put it in `nse/holidays.json`. The scan prints
  a note when it runs outside the years it knows.

## Steps

1. Once a week or so: `scan.py universe` (it also refreshes on its own when the
   cache is older than 7 days).
2. Before the open or after the close: `scan.py daily`. Tell the user the counts
   (fetched, liquid, scanned, hits) and the top few by setup.
3. After 09:35 IST (10:15 is the classic): `scan.py orb`. Tell the user how many
   usable charts broke their range and when, and that a later hit is closer to
   the price now.
4. For delivery ideas: `scan.py delivery`, once the day's bhavcopy is out
   (evening). Say which day it is for.
5. The user picks one chart. Hand it on: `/nse-news TICKER` (what is moving it),
   then `/nse-entry TICKER` (the entry bar by bar). Never pick for them.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data (IST).
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A scan result is a rules check, not a recommendation.
- Paper trading by default. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and
  the user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
  Never set an order tool to "Always allow".
- Intraday (MIS) trades must be closed the same day and carry leverage risk; delivery (CNC) has no
  leverage. The broker's auto square-off is a fee, not a plan.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
