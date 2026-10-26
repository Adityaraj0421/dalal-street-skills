---
name: nse-alerts
description: >-
  Price Alerts and the NSE clock. Rings when a price, a stop, a circuit limit
  or the opening bell hits. Turns the trade plan into alerts in one go (the
  stop, the target, upper/lower circuit proximity and "flat by" at your MIS
  flat-by time, default 15:10 IST), plus above / below / stop / target levels,
  percent moves from the previous close, any time of day, and the 09:15
  opening bell, each ringing once so you are not spammed. Knows NSE hours with
  the 2026 holidays (Diwali, Dussehra, Holi...), weekends and special sessions
  such as Diwali Muhurat trading, answers "is the market open", works out India
  time itself, fetches free delayed quotes from Yahoo (RELIANCE.NS, TCS.NS,
  RELIANCE.BO, Nifty) one ticker at a time, reads each stock's price band from
  NSE's sec_list.csv, and writes nse/prices.json for sizing and the risk check.
  Use when the user says "alert me when", "tell me if RELIANCE hits", "watch my
  stop", "watch my trade", "remind me to square off by 3:10", "is the market
  open", "is NSE open today", "when does the market open", "is it a holiday",
  "is there Muhurat trading", "is it near the upper circuit", "get me a price",
  or right after /nse-exit writes a plan.
argument-hint: "[ticker] [price]"
---

# nse-alerts

On Windows, use `py` in place of `python3`.

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. In a live MIS trade the stop order sits at the
broker and does the work; the alerts tell the user what happened and when it
is time to be flat.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, circuit, flat-by from nse/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1183.65
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --target 1203.55
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:10 --label "flat by" --before-close
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TCS --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol JPPOWER --circuit --within 1
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE --write         # -> nse/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan tickers
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-10-26 15:10"` pins the clock (India time, IST); `--offline DIR`
reads saved `yahoo-<KEY>.json` quote files and `sec_list.csv` instead of the
network.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/plan.json` | the trade from nse-exit (symbol, yahoo, product, stop, target, flat_by), for `add --from-plan` |
| `nse/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `nse/state/alerts.json` | which rules rang and when, so each rings once |
| `nse/state/sec_list.json` | NSE's price band list, fetched once per India date |
| `nse/prices.json` | the latest quotes in rupees, merged in, for nse-budget and nse-risk |
| `nse/portfolio.json` | the tickers held (`positions` or `holdings`), for `quote --holdings` |
| `nse/config.json` | `flat_by` when the plan has none |
| `nse/holidays.json` | the user's calendar additions: next year's holidays, Muhurat timings |

Tickers: `RELIANCE` or `RELIANCE.NS` is NSE, `RELIANCE.BO` is BSE, `NIFTY`,
`BANKNIFTY`, `SENSEX` are indices. In `prices.json` an NSE stock is keyed by
its symbol (`RELIANCE`), a BSE stock by `RELIANCE.BO`, an index by its name;
`detail.<key>.yahoo` and `.exchange` say which listing the price is.

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `circuit` | price within `within` percent of the upper or lower circuit (default: the smaller of 1 % and a quarter of the band), optional `side` | once per side per day |
| `time` | the India clock passed `at` ("15:10") on a trading day | once per trading day |
| `opening_bell` | the NSE is open | once per trading day |

A rule with `"date"` is watched on that India date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today. A `time` rule
with `"before_close": true` moves to 5 minutes before the close on a special
session (Muhurat trading), so "flat by 15:10" does not ring at 18:00 on a day
that closes at 19:00.

A plan gives: a stop alert, a target alert, a circuit alert (NSE stocks with a
fixed band only: for a "No Band" stock such as RELIANCE or TCS the add output says
once that dynamic bands apply and no circuit alert was set; an explicit `--circuit`
on one is refused), and for MIS a flat-by alert. A CNC (delivery) plan is held past today, so it gets
no flat-by alert; say so.

## Circuit limits

NSE's price band for each stock is in `sec_list.csv`
(`https://archives.nseindia.com/content/equities/sec_list.csv`, plain CSV,
cached once a day). The limit is the previous close times (1 plus or minus
the band %), on the 0.05 tick, kept just inside the band. It is a heads-up;
NSE's own page has the exact figure. Bands are 2, 5, 10 or 20 %.

- **"No Band" stocks** (most F&O stocks, including RELIANCE and TCS) have
  dynamic bands. The alert cannot compute a limit; it says so once a day and
  points the user to NSE's website. Do not invent one.
- At a circuit orders can sit unfilled. At the lower circuit there may be no
  buyers, so a stop may not get the user out; at the upper circuit buying more
  may be impossible. The alert says this in one clause. That is the reason
  nse-risk checks the entry, stop and target against the band.
- NSE stocks only: BSE has its own bands (not read here), and an index has no
  circuit alert.

## Quotes

- Yahoo only (Stooq has no Indian tickers). `query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m`,
  an unofficial free JSON endpoint, one request per ticker, half a second apart.
  On a 429 it waits and tries once more; on a 429 or a 5xx after that it tries
  `query1`. `&` in `M&M` is escaped for the URL.
- Free quotes are unofficial, delayed and can lag. They are fine for alerts
  and for checking a size, not for timing an order. The broker's price is the
  one that fills. Each price in `nse/prices.json` keeps its quote time, and
  nse-risk only uses one that is fresh enough.
- Outside market hours the quote is the last trade, and the alert says
  "last", not "is".
- If Yahoo is down, read the price from the broker (Kite) or by hand and write
  it into `nse/prices.json` as `{"as_of": "...Z", "prices": {"RELIANCE": 1195.6}}`.

## The NSE clock

Regular session 09:15 to 15:30 IST (the 09:00 to 09:08 pre-open is not
treated as open). NSE and BSE share the holidays. India has no daylight saving.
The holiday list is the shared one from the NSE circular for 2026: Republic
Day, Holi, Ram Navami, Mahavir Jayanti, Good Friday, Ambedkar Jayanti,
Maharashtra Day, Bakri Id, Muharram, Ganesh Chaturthi, Gandhi Jayanti,
Dussehra (Oct 20), Diwali-Balipratipada (Nov 10), Guru Nanak (Nov 24),
Christmas, and the Jan 15 Maharashtra election day. NSE publishes next year's
list in December: for any other year the clock says the list is unknown and
tells the user to add it to `nse/holidays.json`.

**Closing auction.** From 2026-08-03 to 2026-12-06, F&O stocks stop continuous
trading at 15:15 (an auction follows and takes no stop-loss orders); other
stocks trade to 15:30. From 2026-12-07 everything trades to 15:30 again.
`clock` prints that line, and `clock --symbol RELIANCE` says whether the stock
is an F&O stock and when its continuous trading ends (the list is NSE's open
`fo_mktlots.csv`, cached in `nse/state/fo_mktlots-YYYY-MM-DD.csv`, a cache up
to 7 days old is reused if the fetch fails; unknown means F&O, the cautious
choice, and it says so; `--fo-lots FILE` or `--offline` for a file).

**Flat-by reminder and broker.** `add --from-plan` uses the plan's `flat_by`.
If a plan has none, the reminder is `default_flat_by`: your `flat_by` (15:10),
but 5 minutes before your broker's MIS square-off for that stock at the latest.
The broker is `"broker"` in `nse/config.json` (default zerodha; Zerodha squares
F&O stocks off at 15:12, so 15:07; Dhan at 15:10, so 15:05).

**Muhurat trading** is a special session on Diwali. NSE's 2026 timings are
built in: Sunday 2026-11-08, 14:20 to 15:30 IST (special pre-open 14:05 to
14:20), and F&O stocks stop continuous trading at 15:15 that day. `clock`
shows it up to 45 days ahead and treats the day as a session; the "flat by"
reminder moves to 5 minutes before the close (before the F&O stock's 15:15
end). For another year, add its timings to `nse/holidays.json`
(`"special_sessions": {"2027-10-29": {"open": "18:15", "close": "19:15",
"name": "Muhurat trading"}}`). Never guess the hours.

## Steps

1. Right after nse-exit writes the plan, run `add --from-plan`. Confirm the
   alerts back to the user: the stop, the target, the circuit check, and the
   flat-by time (or that a CNC plan has none).
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the tickers in today's
   rules and rings each alert once. A check reads the latest delayed quote;
   it is not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop or target alert means the
   order at the broker has probably filled: check the order book and open
   positions, and record the fill with nse-broker. "Flat by" means close
   anything still open before the broker's automatic square-off from about
   15:20 (it charges a fee): nse-broker builds the sell ticket, then nse-risk,
   then the user's yes. Never act for them.
4. For "is the market open?", run `clock` and answer from its output, with the
   next open and the holiday name when closed.

## Example

RELIANCE on Monday 2026-10-26: entry ₹1,193.60 at 10:00, stop ₹1,183.65,
target ₹1,203.55, flat by 15:10 (MIS). `add --from-plan` writes
`reliance-plan-stop`, `reliance-plan-target` and `reliance-flat-by` (no circuit
alert: RELIANCE has no fixed band) for that day. A `check` at 10:53 reads the 10:37 quote,
₹1,195.60, between the stop and the target:

```
NSE OPEN (2026-10-26 10:53 IST, regular session).
Nothing hit. 4 rule(s) watched today, RELIANCE ₹1,195.60.
```

At 15:10:

```
ALERT Flat by: it is 15:10 IST (set for 15:10), the market closes at 15:30. If RELIANCE is still open, close it:
an open MIS position is squared off by the broker from about 15:20, with a fee. nse-broker can build the sell
ticket, then nse-risk and your yes.
```

It rings once. The next morning the plan's alerts are no longer watched; with
`add --opening-bell`, the bell rings at 09:15.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: ticker, type, level, day.
- Never invent a holiday, a Muhurat timing or a circuit limit.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, never predicts where a price is going, and never calls NSE's or BSE's
`/api` endpoints (they sit behind bot protection and are not scraped). If
the user wants the exact live circuit limit or the Muhurat timings, send them
to nseindia.com by hand.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
  Never set an order tool to "Always allow".
- Intraday (MIS) trades must be closed the same day; leverage and short selling are the user's call and
  v1 plans are longs only.
- Broker logins stay in the browser or the broker's own setup, never in the chat.
