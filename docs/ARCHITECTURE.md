# Architecture and contributor rules

Dalal Street Skills is 11 Claude skills (`nse-*`), each a standalone folder with a
`SKILL.md` and one Python script, standard library only, Python 3.8+. It began
as a port of Jake Schincariol's US [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT) to Indian equity markets (NSE and BSE). This page is the contract every
script follows. Read it before you open a pull request.

## Ground rules

1. **House style.** Pure `evaluate()`-style functions, `--now` and
   `--offline` for reproducible runs, `--json`, clear exit codes, refusals that
   say why, plain-English output, and "Nothing was placed." where relevant.
2. **Shared India layer.** `_shared/india_core.py` is the source of truth for
   IST time, the NSE calendar, symbols, Yahoo fetching, rupee formatting, tick
   rounding and charges. Each script PASTES the parts it needs (skills are
   copied as standalone folders, they cannot import `_shared`). Paste
   verbatim; do not fork the holiday table or the charges.
3. **Data sources: only these.** All tested working on 2026-10-26 with a plain
   user agent:
   - Yahoo chart: `https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m`
     (`.NS` NSE, `.BO` BSE by symbol e.g. `RELIANCE.BO`; BSE numeric codes like
     `500325.BO` do NOT work on Yahoo. Indices `^NSEI`, `^NSEBANK`, `^BSESN`).
     Escape `&` (M&M.NS) and `^` with `yahoo_quote()`. Bars are on a 5-minute
     grid from 09:15 IST; `meta.gmtoffset` is 19800.
   - Yahoo spark (many symbols, daily): `/v8/finance/spark?symbols=A.NS,B.NS&range=3mo&interval=1d`
   - Yahoo fundamentals: `https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/RELIANCE.NS?type=quarterlyTotalRevenue,quarterlyNetIncome,quarterlyDilutedEPS,...&period1=..&period2=..`
     (values in INR; quarters can have gaps: say so, never invent one).
   - NSE archives (plain CSV, open):
     - `https://archives.nseindia.com/content/equities/EQUITY_L.csv` all NSE equities
       (SYMBOL, NAME OF COMPANY, SERIES, ...). Columns have leading spaces: strip.
     - `https://archives.nseindia.com/content/equities/sec_list.csv` price band
       (circuit %) per symbol: `Symbol,Series,Security Name,Band,Remarks`
       (Band is 2/5/10/20 or "No Band" for F&O stocks; Remarks carry GSM/ASM stages).
     - `https://archives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv`
       daily bhavcopy with DELIV_QTY and DELIV_PER (delivery %).
     - `https://archives.nseindia.com/content/equities/bulk.csv` and `block.csv`
       today's bulk / block deals.
   - Nifty index constituents: `https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv`
     (also `ind_nifty100list`, `ind_nifty200list`, `ind_nifty500list`,
     `ind_niftymidcap150list`, `ind_niftysmallcap250list`, `ind_niftybanklist`).
   - Google News RSS: `https://news.google.com/rss/search?q=<query>&hl=en-IN&gl=IN&ceid=IN:en`
   **Never** call `www.nseindia.com/api/*` or `api.bseindia.com/*`: they are
   behind bot protection and must not be scraped (no cookie games, no spoofed
   browser headers). Use the `UA` string from india_core. If something useful
   only lives there, say so in the SKILL.md and tell the user where to look by hand.
4. **Offline fixtures.** Save small real fixtures you fetch into
   `tests/fixtures/<skill>/` and write `tests/test_<name>.py` (unittest, like
   the US `tests/`) that runs fully offline. Run them with `py -m unittest`.
   You MAY fetch live data with your own scripts to build fixtures.
5. **Windows.** Call `utf8_stdout()` first thing in `main()` (the ₹ sign
   crashes a cp1252 console otherwise). In SKILL.md, write commands as
   `python3 "${CLAUDE_SKILL_DIR}/x.py" ...` and add one line near the top:
   "On Windows, use `py` in place of `python3`."

## Market facts (use these, do not re-guess)

- Time: IST = UTC+5:30, no daylight saving. All user-facing times are IST.
  `--now` is IST.
- Session: 09:15-15:30 IST (pre-open 09:00-09:08 is ignored). Holidays from
  `NSE_HOLIDAYS`; `SPECIAL_SESSIONS` for Muhurat trading (2026-11-08, timings
  TBD, user adds them in `nse/holidays.json`). Call `load_calendar()` once.
  Outside `CALENDAR_YEARS` print `calendar_note()`.
- Opening range default: first 15 minutes (09:15, 09:20, 09:25 five-minute bars).
- Defaults in `nse/config.json`: `no_entries_before` 09:30, `no_entries_after`
  14:45, `flat_by` 15:10 (Zerodha auto-squares-off MIS equity at ~15:20 with
  a fee; flat_by must stay before 15:20). Special-session days: entries
  allowed from open+15 min to close-15 min, flat_by = close-5 min.
- Products: `MIS` = intraday (must be closed same day, leverage allowed,
  shorting allowed but v1 plans LONGS ONLY like the US pack), `CNC` =
  delivery (no leverage, no short selling, T+1 settlement). Every plan/order
  carries `product`.
- Series: `EQ` normal. `BE`/`BZ` = trade-for-trade: delivery only, MIS is
  refused. `SM`/`ST` SME. Stocks under GSM/ASM (sec_list Remarks) get a warning,
  and nse-risk blocks MIS on them.
- Circuit (price band): from sec_list.csv. A price outside
  prev_close × (1 ± band%) cannot trade; "No Band" stocks have dynamic bands
  (warn only). nse-risk checks entry, stop and target against the band.
- Tick size: round with `tick_down` / `tick_up` (0.05, always valid). Long
  stop rounds DOWN, long entry limit rounds UP, long target rounds DOWN.
- Leverage: `mis_leverage` default 5 (SEBI peak-margin rules: 20% margin;
  actual varies by stock, the user should check their broker). CNC: 1.
- Charges: `trade_charges()` (Zerodha defaults, overridable in config
  `charges`). The **max loss of a trade = (entry - stop) × qty + round-trip
  charges at the stop**. Reward:risk uses charges on both outcomes. Show
  charges as their own line: the user should see what the government and
  broker take.
- Money: `inr()` (₹12,34,567.89) and `lakh_crore()` for big numbers.
  Quantities are whole shares.
- Brokers: no Alpaca/IBKR. Primary: **Zerodha Kite** via the official Kite MCP
  server (github.com/zerodha/kite-mcp-server, hosted at `https://mcp.kite.trade/mcp`,
  login through Kite in the browser, no API key in chat). Its order tool is
  `place_order` (verify names when connected; do not hardcode beyond the
  ticket JSON). Zerodha has NO paper trading and no bracket orders: nse-broker
  keeps a built-in **paper ledger** (default mode) and, for live, an entry
  order plus an `SL-M` stop order (MIS) or a GTT OCO stop+target (CNC).
  Mention Upstox/Dhan/Angel One as "check whether your broker has an
  official MCP/API" without inventing details.

## Order contract (`nse/orders.json`, written by nse-budget, read by nse-risk / nse-broker)

```json
{
  "created_by": "nse-budget",
  "created_at": "2026-10-26T04:12:00Z",
  "orders": [{
    "id": "RELIANCE-20261026-0945",
    "symbol": "RELIANCE", "exchange": "NSE", "yahoo": "RELIANCE.NS", "series": "EQ",
    "product": "MIS",
    "entry": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
              "transaction_type": "BUY", "quantity": 40, "product": "MIS",
              "order_type": "LIMIT", "price": 1196.10, "validity": "DAY"},
    "stop":  {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
              "transaction_type": "SELL", "quantity": 40, "product": "MIS",
              "order_type": "SL-M", "trigger_price": 1188.50, "validity": "DAY"},
    "target": {"price": 1211.30, "how": "LIMIT sell placed after the fill, or GTT OCO for CNC"},
    "gtt_oco": null,
    "plan": {"entry": 1196.10, "stop": 1188.50, "target": 1211.30, "r_per_share": 7.60},
    "max_loss": 345.12, "charges_at_stop": 41.12, "charges_at_target": 41.50,
    "reward_risk": 1.85, "margin_needed": 9568.80, "flat_by": "15:10",
    "fingerprint": "sha1 of the entry+stop legs"
  }]
}
```
For CNC, `stop` is null and `gtt_oco` holds
`{"trigger_type": "two-leg", "tradingsymbol", "exchange", "trigger_values": [stop, target], "last_price", "orders": [sell@stop, sell@target]}`.

## Working files (folder `nse/`, not `trader/`, so it never collides with the US pack)

| file | writer | readers |
| --- | --- | --- |
| `nse/config.json` | user | all (budget, limits, times, charges, broker) |
| `nse/holidays.json` | user | all (calendar overrides) |
| `nse/watchlist.json` | nse-scanner | nse-news, nse-entry |
| `nse/entry.json` | nse-entry | nse-exit |
| `nse/plan.json` | nse-exit | nse-timemachine, nse-budget, nse-alerts |
| `nse/orders.json` | nse-budget | nse-risk, nse-broker |
| `nse/prices.json` | nse-alerts | nse-budget, nse-risk |
| `nse/portfolio.json` | user / nse-broker | budget, risk, bigmoney (holdings) |
| `nse/state/*.json` | each skill | its own state (seen news, risk verdicts, broker log, alerts fired) |

Field names in these files should match the US pack's equivalents unless an
Indian fact forces a change (then add the field, e.g. `product`, `exchange`).

## Skill map

| US | India | notes |
| --- | --- | --- |
| trader-scanner | nse-scanner | universe: Nifty index lists or EQUITY_L (EQ series); daily setups via spark; ORB scan on 5m; plus **delivery % spike** from bhavcopy (investor signal) |
| trader-news | nse-news | Google News RSS per company name (from EQUITY_L), tags incl. results, dividend/bonus/split, block deal, SEBI, order win, rating; moves vs prev close. Yahoo RSS is empty for India: don't use it |
| trader-entry | nse-entry | ORB from 09:15, cutoffs above |
| trader-exit | nse-exit | ticks 0.05, ATR/percent/range stops, refuses a stop inside the spread; adds product + flat_by |
| trader-timemachine | nse-timemachine | 09:15-15:30 sessions, flat_by exit, charges in R |
| trader-budget | nse-budget | sizes to max loss incl. charges, margin with mis_leverage, writes the order contract |
| trader-risk | nse-risk | US checks + series/BE, circuit band, GSM/ASM, MIS cutoff, CNC no-short, charges |
| trader-broker | nse-broker | paper ledger default; Kite MCP setup; two yeses + "Needs approval" card |
| trader-alerts | nse-alerts | NSE hours/holidays/Muhurat, Yahoo only (no Stooq for India) |
| trader-analyst | nse-analyst | Yahoo fundamentals timeseries: revenue, net income, EPS, margins, YoY, TTM, in crore |
| trader-pelosi | nse-bigmoney | NSE bulk + block deals: who bought/sold what, flags your holdings/watchlist, keeps history in state. SEBI insider (PIT) disclosures live only on nseindia.com/api: document the manual route, do not scrape |

## Done means

- `nse-<name>/SKILL.md` + script, frontmatter `name` and a trigger-rich
  `description` in the US style but Indian (RELIANCE, TCS, Nifty, ₹, MIS/CNC).
- Offline tests pass with `py -m unittest discover -s tests -p "test_<name>.py"`
  run from the repo root.
- One live smoke run of the script against today's real data, output checked.
- No US leftovers: grep your files for `NYSE`, `New York`, `ET`, `$`, `SEC`,
  `Alpaca`, `trader/` and fix each hit (except deliberate mentions).

## v1.1 changes (2026-10-26): closing auction + ten brokers

### Closing Auction Session (CAS) — affects every time-aware skill
- From 2026-08-03, **F&O stocks** ("CAS stocks") stop continuous trading at
  **15:15**; an auction runs after that (NSE/CMTR/73362, go-live NSE/CMTR/75479).
  Stop-loss orders are not accepted in the auction. Other stocks trade to 15:30.
- From **2026-12-07** (SEBI circular 2026-10-15) continuous trading runs to
  15:30 for every stock again (auction 15:30-15:40).
- `india_core.continuous_close(day, cas_stock)` gives the right end time. Use it
  instead of the fixed `CLOSE` wherever a stock's session end matters
  (entry cutoffs, ORB scans, replays, alerts, risk windows). This is also
  why Yahoo has no 15:20/15:25 bars for F&O stocks since August.
- Which stocks are CAS stocks: `parse_fo_symbols()` on
  `https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv` (open file).
  Cache it as `nse/state/fo_mktlots-YYYY-MM-DD.csv` (same name in every skill
  so one download serves all; reuse a cache up to 7 days old if the fetch
  fails, and say so). Fixture: `tests/fixtures/_shared/fo_mktlots.csv`.
- Muhurat 2026-11-08 is now in `SPECIAL_SESSIONS` (14:20-15:30).

### Brokers
- `india_core.BROKERS` has the top 10 by NSE active clients (groww, zerodha,
  angelone, icicidirect, upstox, kotak, hdfcsky, dhan, sbi, motilal), chosen
  by `"broker"` in nse/config.json (default zerodha). Each profile: brokerage
  (pct, min, cap) for MIS and CNC, DP charge, MIS square-off times (cas/other),
  stop mechanisms, API, MCP, verified fields.
- `trade_charges(..., cfg=cfg)` now uses the configured broker's brokerage
  (pass the loaded config!). `charges_cfg` keys gained `brokerage_min_*`.
- `squareoff_time(day, cas_stock, cfg)` -> (time, note) and
  `default_flat_by(day, cas_stock, cfg)` -> (flat_by, squareoff, note):
  flat_by = min(config flat_by or 15:10, square-off - 5 min). Print the note
  when present (unverified or possibly outdated square-off times).
- Full research with sources: `docs/brokers.md` (order field names per
  broker API, MCP capabilities, SEBI retail-algo rules).
