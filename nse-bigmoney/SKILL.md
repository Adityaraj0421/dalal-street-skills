---
name: nse-bigmoney
description: >-
  The Big Money Tracker for Indian markets. Reads NSE's public bulk deal and
  block deal files, keeps every day it sees, and shows who bought or sold what:
  client, stock, quantity, price and value in rupees crore. Alerts once per new
  deal, flags stocks in your portfolio or watchlist, filters by client name
  (a fund, an investor, an AMC), ranks the day's biggest deals, and summarises
  one client's buys and sells across stored history. Documents the manual route
  for SEBI insider (PIT) disclosures and shareholding patterns. Use when the
  user says "bulk deals today", "block deals", "what is Rakesh Jhunjhunwala's
  family buying", "which fund bought X", "who is buying RELIANCE", "smart
  money", "FII DII buying", "insider buying", "promoter buying", "alert me on
  big deals in my stocks", or "run the big money skill".
argument-hint: "[client name] [--symbol X]"
---

# nse-bigmoney

On Windows, use `py` in place of `python3`.

When someone buys or sells a large slice of a company, NSE publishes it. This
skill reads those files, remembers each day, and tells you what is new. It is
fast. It is not early: the files appear after the close, so you read a deal
hours old at best. There is no inside information here, only public data read
quickly. Say that every time.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/deals.py" --help
```

## What it reads and writes

- Reads `https://archives.nseindia.com/content/equities/bulk.csv` and
  `block.csv`: plain open files, one trading day each, replaced the next day.
- Writes `nse/state/deals.json`: every deal ever seen (the history) and which
  ones were already shown, so each deal alerts once.
- Reads `nse/portfolio.json` and `nse/watchlist.json` (if they exist) to flag
  deals in stocks you hold or follow; `--tickers` adds more.

## What these deals are

- **Bulk deal**: one client's total traded in a stock in a day is above 0.5%
  of the company's listed shares.
- **Block deal**: one large order done in NSE's special block windows, above a
  minimum order size set by the exchange and SEBI (check the current size on
  nseindia.com).
- Value is quantity times the weighted average price in the file, shown in
  lakh/crore. The file does not give the stake as a percentage of the
  company, so the tool does not either.

## The rules

- **Quote the file.** Each line comes from NSE's CSV with its date. If the
  file is days old (weekend, holiday, a missed run) the tool says so.
- **It only accumulates if you run it.** NSE's files hold one day. This tool
  does not backfill. Run it each trading evening (`/loop 1d` or a scheduled
  task) and the history grows; a day you skip is missing from `who`. The
  manual route for old days: nseindia.com > Market Data > Historical Data >
  Bulk Deals / Block Deals, which lets you download a date range by hand.
- **The client is not always "smart money".** Names include mutual funds, FPIs
  and rich investors, but also brokers, prop and arbitrage desks (a client that
  buys and sells the same stock the same day is usually a trading desk, not a
  conviction bet), market makers and promoters. A buy is not a verdict.
- **A deal has two sides.** Every purchase has a seller. A fund buying from a
  promoter is not the same as a fund buying from another fund.
- **The tool combines rows.** NSE splits a client's day into several rows; the
  default view adds them up (quantity added, price weighted). `--raw` shows
  every row.
- **No return claims.** Copying a deal does not beat the market.

## Step 1: what is new

```bash
python3 "${CLAUDE_SKILL_DIR}/deals.py" new
python3 "${CLAUDE_SKILL_DIR}/deals.py" new --client "nippon" --client "quant"
python3 "${CLAUDE_SKILL_DIR}/deals.py" new --min-crore 50 --kind block
```

Shows deals not shown before, biggest first, flags your stocks, and records
them. Exit code 1 means nothing new. `--client` is a case-insensitive
substring of the client name and repeats; filtered-out deals stay unseen for the
next unfiltered run. `--symbol`, `--side BUY|SELL`, `--limit 0` for all rows,
`--json` for machine output, `--dry-run` to write nothing.

## Step 2: the day's biggest, a stock, a client

```bash
python3 "${CLAUDE_SKILL_DIR}/deals.py" top --days 5 --n 10
python3 "${CLAUDE_SKILL_DIR}/deals.py" show --symbol RELIANCE --days 30
python3 "${CLAUDE_SKILL_DIR}/deals.py" who "Jhunjhunwala" --days 90
```

`top` ranks by value over the last N stored days. `who` totals one client's
buys and sells per stock across stored history and says "net buyer" or "net
seller" per stock. It always states how many days the history holds: with one
day, say that the picture is thin.

## What to say

Give the line, the date, and the caveat. Example from the 2026-10-23 file:
"Graviton Research Capital sold 1.44 crore Swiggy shares at about ₹184 (₹265 crore) and bought 1.42 crore
at about ₹184 the same day: that is a trading desk, not a view." and "Yali
Deeptech Fund I sold 24 lakh Tonbo shares at about ₹1,069, ₹257 crore". If the
user holds the stock, point it out, then send them to the chart and the
filings, not straight to an order.

If the user wants to act on it, it goes the long way: a disclosure is hours or
days old, so it is a reason to look, not a setup. `/nse-scanner` and
`/nse-entry` decide if there is a trade today; then `/nse-budget`,
`/nse-risk` and `/nse-broker` as always, MIS or CNC.

## Insider (SEBI PIT) disclosures and shareholding patterns: the manual route

These are not in the open archive files; they live behind nseindia.com's
bot-protected API and BSE's site, so this skill does **not** scrape them. Where
to read them by hand:

1. **Insider trades (SEBI PIT)**: nseindia.com > Companies Listing >
   Corporate Filings > Insider Trading / PIT; or bseindia.com > Corporate
   Announcements, category "Insider Trading / SEBI (PIT)". Promoters, directors
   and designated persons must disclose within two trading days of a trade
   above the threshold.
2. **Shareholding pattern** (promoter, FPI, mutual fund, public holding):
   nseindia.com > Corporate Filings > Shareholding Pattern, or the company's
   page on bseindia.com, filed within 21 days of each quarter end. Pledged
   shares are in the same filing.
3. Paste the table (or a screenshot, or the saved page) into the chat, and write it
   up in the same shape: who, what, how many shares, the filing link and date,
   and the same caveat that it is a lagging public disclosure. Aggregators like
   screener.in are fine for browsing, but the exchange filing wins.

## What it never does

- Never says or implies a client has inside information, or that following
  big money beats the market.
- Never presents a deal as a prediction, or a broker's name as a conviction buy.
- Never scrapes nseindia.com/api or bseindia.com; never places an order.

## Safety

Education and tooling, not investment advice. Paper trading is the default in
`nse/config.json`. Nothing places an order except `/nse-broker`, and only after
`/nse-risk` passes AND the user types an explicit yes for that order in the
chat, AND the broker tool is set to "Needs approval" in Claude so its own
approval card appears too. Bulk and block deals are public data published
after the close; this skill reads them fast and has no inside information.
