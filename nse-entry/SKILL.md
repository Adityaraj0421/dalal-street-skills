---
name: nse-entry
description: >-
  Entry Timer for NSE/BSE. Watches a setup bar by bar and tells you the moment
  to get in. Reads free Yahoo 1-minute or 5-minute bars, builds the opening
  range (the first 15 minutes from 9:15 IST by default) and calls the entry on
  the first completed bar that CLOSES above the range high, or above a level
  you give it (like a 20-day high from the scanner), with an optional volume
  check. Says waiting and how far, triggered with the time and the price in
  rupees, expired, or not open yet, and writes nse/entry.json for /nse-exit.
  Works for RELIANCE, TCS, Nifty stocks (.NS or .BO), intraday (MIS) or
  delivery (CNC). Long side only. It never places an order. Use when the user
  says "tell me when to get in", "watch RELIANCE", "has TCS broken out", "did
  it break the opening range", "entry signal", "time my entry", "check my
  watchlist", or after /nse-scanner and /nse-news, before /nse-exit.
argument-hint: "[SYMBOL] [--setup orb|level] [--interval 5m|1m] [--product MIS|CNC]"
---

# nse-entry

On Windows, use `py` in place of `python3`.

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads bars, 1-minute or 5-minute, and waits for each one to finish. A price
that pokes above the high in the middle of a bar is not an entry. A bar that
CLOSES above it is. Then it says the time and the price, writes them down for
the Exit Planner, and stops. It never buys anything. Day trading loses most
people money, and a clean signal does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE                 # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --write         # also save nse/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check TCS --setup level --level 2525
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE.BO --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" check INFY --product CNC --write  # delivery instead of intraday
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch RELIANCE --until 14:45   # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                      # every hit in nse/watchlist.json
```

Symbols: `RELIANCE` or `RELIANCE.NS` (NSE), `RELIANCE.BO` (BSE). BSE numeric
codes like 500325 do not work on Yahoo, use the name. `M&M` is fine. An index
(`NIFTY`) is refused: you cannot buy it, pick a stock or an index ETF.

Other flags: `--or-minutes N` (default 15), `--json`, `--offline FILE` (a
saved Yahoo chart JSON, `.json` or `.json.gz`, or a folder of
`<SYMBOL.NS>.json`), `--now "2026-10-26 10:00"` (India time, IST) to replay a
day. `watch` with `--now` or `--offline` runs on a simulated clock and replays
the day bar by bar without waiting.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/watchlist.json` | the scanner's hits (`hits[].symbol`, optional `yahoo`, `level`), for `watchlist` |
| `nse/config.json` | `no_entries_before` (09:30) and `no_entries_after` (14:45), India time, and `entry_buffer_min` (45). Optional |
| `nse/holidays.json` | optional calendar additions: holidays, and special sessions such as Muhurat trading |
| Yahoo chart API | today's bars, `range=1d&interval=5m` (or `1m`): free, unofficial, can lag |
| `nse/entry.json` | with `check --write`, or when `watch` triggers: symbol, exchange, yahoo, product, setup, status, time, bar, price, level, opening range |

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes: the
  9:15, 9:20 and 9:25 five-minute bars, or fifteen 1-minute bars. The level
  to beat is the range high.
- **Level.** A price you give, like a 20-day high from `/nse-scanner`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time (the 9:55 bar closes at 10:00 AM).
  The price is its close, to the paisa.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Yahoo has caught up to that moment (it waits a
  minute at most). Yahoo's live tick, like 10:27:20, is never a bar.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. The 9:15 bar holds the
  opening auction, so 1.0 is already strict.
- **Time limits.** No entries before 9:30 or after 14:45 (config), and never
  later than `entry_buffer_min` (config, default 45) minutes before continuous
  trading ends, the same cutoff nse-risk enforces: 14:30 for F&O stocks while
  the closing auction rule runs, 14:45 otherwise. On a special session
  (Muhurat trading, built in: Sunday 2026-11-08, 14:20 to 15:30) entries run
  from 15 minutes after the open to 15 minutes before the close, whatever the
  config says. Add other years' timings to `nse/holidays.json`.
- **Closing auction.** From 2026-08-03 to 2026-12-06, F&O stocks stop
  continuous trading at 15:15 (an auction follows, and it takes no stop-loss
  orders). Other stocks trade to 15:30. From 2026-12-07 everything trades to
  15:30 again. The output says when the auction shortens the day. Which stocks
  are F&O comes from NSE's open `fo_mktlots.csv`, cached in
  `nse/state/fo_mktlots-YYYY-MM-DD.csv` (a cache up to 7 days old is reused if
  the fetch fails, with a note; if the list cannot be loaded at all every stock
  is treated as an F&O stock, the cautious choice, and the output says so). `--fo-lots FILE` gives the list by hand, and
  `--offline` uses the test copy.
- **Product.** `--product MIS` (default, intraday: closed the same day, leverage
  allowed) or `CNC` (delivery: no leverage, no short selling). It is only
  written into `nse/entry.json` for the next steps. Trade-for-trade series
  (BE, BZ) cannot be MIS: `/nse-risk` refuses that, this skill does not check.
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before 9:15: not open yet. Before 9:30: the
  range is still forming. Weekends and NSE holidays: the market is closed
  (the holiday list covers 2026; outside it, it says so).

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/nse-scanner`. Check `/nse-news` for it first.
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 14:45` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `nse/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade.
5. Hand off to `/nse-exit` to set the stop and target BEFORE anything
   is bought.

## Example: the reel

RELIANCE, Monday 2026-10-26, 5-minute bars. The opening range is 1175.30 to
1192.00. At 9:58 the 9:55 bar is still open, so it waits:

```
WAITING  last close ₹1,186.40 (9:50 bar), ₹5.60 under the range high ₹1,192.00 (-0.47%).
The 9:55 bar is still forming (₹1,193.60 so far, closes 10:00 AM); it does not count until it closes.
```

At 10:00 the bar has closed:

```
RELIANCE.NS  Mon 2026-10-26 10:00 IST  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag)
Opening range 9:15-9:30: high ₹1,192.00, low ₹1,175.30 (3 of 3 bars).
TRIGGERED 10:00 AM at ₹1,193.60: the 9:55 bar (bar 8) closed above the range high ₹1,192.00 (+0.13%).
Product MIS (intraday): it must be closed the same day, and nse-exit sets the flat-by time.
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /nse-exit (exit.py plan --from nse/entry.json).
```

## Rules

- Bar by bar, on free Yahoo 1-minute or 5-minute bars. They are unofficial
  and can lag. Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only. v1 plans longs only. If the user wants a short, say that.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.
- Circuit limits are not checked here: a stock at its upper circuit may not
  fill. `/nse-risk` checks the price band.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/nse-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/nse-broker` from a background
watch: nobody is there to say yes. It never calls nseindia.com or
bseindia.com APIs.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`nse-broker` keeps a built-in paper ledger). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, futures, short selling or leverage beyond MIS unless the config allows it and the user confirms.
- Broker keys and logins stay in the broker's own login or the Claude Desktop config, never in the chat.
