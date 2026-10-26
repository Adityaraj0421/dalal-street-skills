---
name: nse-analyst
description: >-
  The Results Analyst for Indian stocks. Reads a listed company's reported
  numbers from Yahoo's fundamentals time series and sums up what changed:
  revenue, gross, operating, EBITDA and net margins, net profit and EPS, quarter
  by quarter in rupees crore, each against the same quarter a year earlier
  (Q1FY27 = April-June 2026), with the trailing twelve months, the last fiscal
  years, and balance sheet and cash flow lines. Missing quarters are shown as
  gaps, never filled in. Reads a concall transcript when you paste one. Use when
  the user says "how did Reliance's results look", "TCS Q2 results", "analyze
  the quarterly results", "what changed this quarter", "is profit growing",
  "compare margins", "sum up the concall", "when does Infosys report", or before
  trading or investing in a stock around its results (MIS or CNC, Nifty
  stocks on NSE or BSE).
argument-hint: "[symbols]"
---

# nse-analyst

On Windows, use `py` in place of `python3`.

Results day moves Indian stocks more than almost anything else, and the press
release puts the best number first. This skill lines up the numbers quarter by
quarter and says plainly what changed. For an intraday trader it answers: what
did the last report say, and does the next one fall inside this trade? For a
delivery (CNC) investor it shows the multi-year trend and the cash flow behind
the profit.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/facts.py" --help
```

## What it reads and writes

- Reads Yahoo's fundamentals time series
  (`query2.finance.yahoo.com/ws/fundamentals-timeseries/...`) for `SYMBOL.NS`
  (or `.BO`). Values come in the company's reporting currency (rupees for most) and rupees are shown in crore (1 crore =
  1,00,00,000), grouped the Indian way (3,09,468).
- Writes `nse/metrics.json` with `--metrics`: one row per company (TTM revenue,
  net profit and EPS in rupees, latest margins and growth) for notes or a sheet.
- Never calls nseindia.com/api or bseindia.com APIs: they sit behind bot
  protection and are not scraped. The official numbers are the **results
  filing on NSE** (nseindia.com > Corporate Filings > Financial Results) or
  **BSE** (bseindia.com > Corporate Announcements > Results) and the company's
  investor page. The tool says so under every report; for anything you will quote or
  trade on, open the filing.
- Concalls: the tool does not fetch transcripts. If the user pastes one (or
  the investor page has it), read it and sum up what management said changed:
  guidance, margins, demand, order book, anything they declined to answer.
- The exact board-meeting date is not in Yahoo's data. The tool prints the
  latest date the next results can come (SEBI: 45 days after a quarter, 60 for
  the March quarter); the actual date is on the NSE/BSE announcements or in
  `/nse-news` headlines. Say where it came from.

## Yahoo fundamentals keys that work for Indian stocks

Checked on RELIANCE.NS and TCS.NS on 2026-10-26. Quarterly and annual:
`TotalRevenue`, `CostOfRevenue`, `GrossProfit`, `OperatingIncome`, `EBITDA`,
`NetIncome`, `DilutedEPS`, `BasicEPS` (prefix `quarterly` or `annual`). Annual
only: `TotalDebt`, `StockholdersEquity`, `CashAndCashEquivalents`,
`OperatingCashFlow`, `FreeCashFlow`, `CapitalExpenditure`, `TotalAssets`
(quarterly balance sheet keys return one or two points at most, and quarterly
`OperatingCashFlow` returns nothing, so they are not used).

## The rules

- **Gaps are gaps.** Yahoo often lacks a quarter (both RELIANCE and TCS lack
  Q2FY26, September 2025). The table prints "not on Yahoo", nothing is
  interpolated, and a growth number appears only when the exact same quarter a
  year earlier exists.
- **Trailing twelve months only from four consecutive quarters.** A gap
  inside the last four means no TTM, and the tool says which quarter is missing.
  Fiscal-year columns are Yahoo's own annual figures.
- **Compare like with like.** Growth is against the same quarter a year
  earlier. Quarter-on-quarter is shown, but many Indian businesses are seasonal
  (festive demand, monsoon, year-end billing).
- **Consolidated vs standalone is not stated by Yahoo.** Say so and check the
  filing before quoting. Banks, NBFCs and insurers report revenue and
  "operating income" differently: margins for them can be blank or
  misleading, and Yahoo's "gross profit" is often derived from cost of revenue
  (the tool marks that).
- **Flag one-offs.** When net profit is above operating income, income outside
  the core business (treasury income, exceptional or tax items) is inside it.
  Reliance's Q1FY27 profit is -22% against a year earlier while revenue is up
  27%: the year-ago quarter was unusually high, which is why you read the
  filing.
- **Say when Yahoo lags.** If results are already past their SEBI deadline and
  Yahoo has not caught up, the tool prints a NOTE.
- **Currency comes from the data, not from the exchange.** Yahoo marks every
  point with a currency code. Most Indian companies report in rupees, but some
  (Infosys reports in US dollars) do not. Those are shown in their own
  currency under a header such as "Amounts in USD million", with no rupee
  sign. `--inr` converts them at the USD/INR close (Yahoo `USDINR=X`) on or
  before each period end, prints the rates used, and warns that growth rates
  then include currency moves. Points in a different currency from the rest of
  a stock's series are left out, never blended. Checked 2026-10-26: INFY is
  USD; WIPRO, HDFCBANK, ICICIBANK, DRREDDY, TCS and RELIANCE are INR.
- **Quarters** are labelled by Indian fiscal year: Q1FY27 = April-June 2026,
  Q4FY27 = January-March 2027.

## Steps

1. Run it for the companies in question (or the symbols on
   `nse/watchlist.json`):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE
   python3 "${CLAUDE_SKILL_DIR}/facts.py" TCS INFY HDFCBANK --metrics nse/metrics.json
   ```

   `--quarters 8` shows two years, `--json` everything machine-readable,
   `--inr` converts a company that reports in dollars (INFY) to rupees.
2. Open the filing on NSE/BSE (or the investor page) when the numbers need
   context, and read it.
3. Write the summary: one line on the quarter (revenue, net profit, EPS,
   growth), one on margins, one on anything unusual (a one-off, a gap, a lag),
   and where the official numbers are. Plain words, real numbers, no adjectives
   the numbers do not support.
4. If results fall inside the planned trade (an overnight hold, or a report
   before the open), say it before `/nse-exit` sets the stop: a stop does not
   protect against a gap, and an MIS position is closed the same day anyway.

## Example

`facts.py RELIANCE TCS` (run 2026-10-26): RELIANCE Q1FY27 (quarter ending
2026-06-30) revenue 3,09,468 crore (+27.0% vs a year earlier), net profit
20,946 crore (-22.4%), EPS 15.48; Q2FY26 is a gap so there is no TTM. TCS
Q2FY27 revenue 73,188 crore, net profit 13,884 crore, EPS 38.37 but no growth
figure (Q2FY26 absent); trailing four quarters 2,83,248 crore revenue, EPS
142.66. The summary ends with the filing links and "TCS's next results are
due by 2027-02-14 at the latest".

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell.
- Never invents a number or estimates a quarter it does not have.
- Never scrapes nseindia.com or bseindia.com.

## Safety

Education and tooling, not investment advice. This skill only reads data.
Nothing places an order except `/nse-broker`, after `/nse-risk` passes and the
user types an explicit yes for that order.
