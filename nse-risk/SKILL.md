---
name: nse-risk
description: >-
  Risk Manager for NSE/BSE. Checks every trade against your limits and stops
  anything over them, before it can reach the broker. For an intraday (MIS)
  trade: a stop is required, the loss at the stop INCLUDING charges must fit
  your max loss per trade, reward:risk after charges must meet your minimum,
  the day's total risk must fit your daily loss limit, no more than your
  trades per day, entries only inside your window (IST, NSE holidays and
  Muhurat sessions), no entry in the last minutes before continuous trading
  ends (F&O stocks stop at 15:15 until 2026-12-06), and the order must carry
  a flat_by at least 5 minutes before the configured broker's MIS auto
  square-off for that stock and day. India rules: series BE/BZ cannot be MIS, GSM/ASM stocks
  block MIS and warn on delivery, entry/stop/target must sit inside the
  circuit band from sec_list.csv, CNC can never be sold short. Plus the
  basics: blocked symbols, order size, position cap, cash buffer and MIS
  margin, F&O, malformed orders and GTTs, duplicates and a daily order limit.
  Getting out is never blocked. Prints PASS or BLOCK with the reason and
  records the verdicts so nse-broker only shows an order that passed. Use when
  the user says "check my order", "is this safe", "risk check", "does this
  break my rules", "can I take this trade", "is RELIANCE in a circuit",
  "how much have I risked today", or before /nse-broker, always.
argument-hint: "[path to orders.json]"
---

# nse-risk

On Windows, use `py` in place of `python3`.

The Risk Manager is the gate between a proposal and the broker. Every order
written by nse-budget or nse-broker's flatten passes through it. A BLOCK is
final for that order: it does not go to the user for a yes, and it does not go
to the broker. Most people who trade intraday lose money. These limits keep
each loss and each day's losses small. They do not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # nse/ files; writes nse/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-26 09:45"  # pin the clock (IST)
python3 "${CLAUDE_SKILL_DIR}/check.py" --offline --seclist sec_list.csv   # no network at all
```

## What it reads and writes

| file | what |
| --- | --- |
| `nse/orders.json` | the proposed orders (nse-budget, or nse-broker's flatten) |
| `nse/config.json` | the limits (nse-budget ships `config.example.json`) |
| `nse/portfolio.json` | cash and holdings per symbol and product; if missing, a fresh account funded with the budget |
| `nse/prices.json` | a fresh price raises the entry it measures risk from; `detail.SYMBOL.prev_close` feeds the circuit band |
| `nse/state/broker.json` | what was placed today: duplicates, trades per day, daily loss |
| `nse/state/sec_list.csv` | NSE's series / price band / GSM-ASM list, downloaded once a day (archives.nseindia.com) |
| `nse/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

The only network calls are `sec_list.csv` from NSE's open archives and, for
the circuit band, Yahoo's daily chart for the previous close (skipped if
`nse/prices.json` or the order already has it, or with `--offline`). It never
touches nseindia.com/api. If the list cannot be loaded it says so in a WARNING
and the series, GSM/ASM and circuit checks are NOT done: tell the user.

## Entries and exits

An **entry** opens or adds to a position: a buy, or a sell that would go
short. An **exit** only reduces what you already hold (in that product: MIS
and CNC holdings are separate). Getting out must always be possible, so an
exit is checked for format, asset and short sales only. Every other rule turns
into a warning for an exit, never a BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no symbol, side not BUY/SELL, product not MIS/CNC, quantity not a whole number, LIMIT without a price, SL / SL-M without a trigger, a stop leg that does not match the entry (side, quantity, product, symbol), a bad GTT OCO (needs two-leg, [stop, target], two SELLs), a GTT on an MIS order |
| asset | F&O or a derivatives exchange while `allow_fno` is false; anything that is not an NSE / BSE equity symbol |
| short | a sell of more than you hold. CNC is never allowed short (you cannot sell shares you do not have); MIS only with `allow_short` |
| series | (entries) series BE / BZ (trade-for-trade) with MIS: delivery only |
| gsm / asm | (entries) the sec_list Remarks carry GSM or ASM and the product is MIS. CNC gets a warning |
| circuit | (entries) the entry, the stop or the target is outside previous close x (1 +/- band%) from sec_list.csv. "No Band" stocks (most F&O names, RELIANCE, TCS) have dynamic bands: a warning, not checked |
| blocked | (entries) the symbol is on the `blocked` list |
| size | (entries) order value under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would tie up more than `max_position_pct` of the account |
| cash | (entries) the margin (MIS: value / `mis_leverage`, CNC: the full value) is more than your cash, or leaves less than the cash buffer |
| stop | (entries) MIS: no SL-M stop while `require_stop`. CNC: no GTT OCO while `require_stop_cnc` (default false: a warning). A stop on the wrong side of the entry |
| loss | (entries) quantity x (entry minus stop) PLUS round-trip charges at the stop, over `max_loss_per_trade_inr` |
| reward:risk | (entries) (target gain minus charges) / max loss under `min_reward_risk` |
| flat_by | (MIS entries) no `flat_by` on the order, or it is later than `default_flat_by()` for this stock, broker and day: 5 minutes before the configured broker's MIS square-off (F&O "closing auction" stocks square off earlier until 2026-12-06; from 2026-12-07 everyone is back to the normal time), and never later than your `flat_by`. Unverified or possibly outdated square-off times print a note |
| window | (MIS entries) before `no_entries_before`, after `no_entries_after` or after the end of continuous trading in that stock minus `entry_buffer_min` (45; F&O stocks end at 15:15 until 2026-12-06, so their last entry is 14:30), at or past the order's flat_by, or the market is closed (weekend, NSE holiday, outside 09:15-15:30). Special sessions (Muhurat): from open+15 min to close-15 min, flat_by at most close-5 min. A CNC order when the market is closed only warns (it would queue as an after-market order) |
| daily loss | (MIS entries) the max losses at the stops of today's MIS entries, plus this one, over `max_daily_loss_inr` |
| trades | (MIS entries) `max_trades_per_day` MIS entries already placed today |
| duplicate | (entries) the same symbol, side and product twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a LIMIT order. For a
  market order, the HIGHER of the plan's entry and a fresh price from
  `nse/prices.json` (under 15 minutes old). An older price is not used, and it says so.
- **Charges are in the max loss and in reward:risk**: the same `trade_charges()` as nse-budget (STT,
  brokerage, exchange and SEBI fees, GST, stamp duty, the DP charge for CNC), with the broker profile's
  numbers and any `charges` overrides in `nse/config.json`.
- **Daily loss** counts the full max loss of every MIS entry placed today, win or
  lose, as nse-broker logged it. Three full-size trades use up a Rs 3,000 day. CNC investing is not a
  day trade: it does not count toward trades per day or the daily loss.
- **The window** is India time (IST, no daylight saving), worked out in the tool.
  Weekends and NSE holidays are closed. Set both `no_entries_*` to null to switch the MIS window off.
  Outside 2026 the holiday list is incomplete and it says so.
- **Circuit**: bands are 2 / 5 / 10 / 20 percent of the previous close, on the 0.05 tick grid. A stop outside
  the band could never trigger; a target outside it could never fill.

Warnings do not block, but say them to the user: a LIVE account, a stop that
lives only in the plan and not in an order, a price too old to use, a symbol
missing from sec_list.csv, a GSM/ASM delivery buy, "No Band" stocks, BSE orders (the NSE band list does
not cover BSE), an unknown broker profile.

## Steps

1. If the price may have moved since the plan, refresh it first
   (nse-alerts: `alerts.py quote RELIANCE --write`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop with the charges line, reward:risk, and how much of today's limits it uses. For a
   BLOCK, give the reason and the fix (fewer shares, a closer stop from
   nse-exit, wait for the window, delivery instead of MIS, or skip the trade). Never change a limit yourself.
4. If the order changes in any way, run the check again. nse-broker matches
   each order to its verdict by fingerprint, so an edited order is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example: the reel

The RELIANCE order from nse-budget at 09:45 on Monday 2026-10-26, fresh Rs 2,00,000 paper account, example config:

```
PASS  #1 buy RELIANCE 118 shares MIS, entry
          entry 1196.10 (the limit price), stop 1188.50, target 1211.30: risk 7.60 a share
          max loss ₹993.82 = ₹896.80 + charges ₹97.02 (limit ₹1,000), reward:risk 1.71 after charges (min 1)
          order value ₹1,41,139.80, margin ₹28,227.96 at 5x leverage
          today before this: 0 of 3 MIS trades, ₹0.00 of ₹3,000 daily loss
        ~ RELIANCE has no fixed price band (dynamic bands apply): the circuit check is skipped
```

One more share (119) is blocked: 119 x 7.60 is only Rs 904.40, but with charges the loss at the stop is over
Rs 1,000. At 09:20 it is blocked by the window (no entries before 09:30); at 14:31 it is blocked too, because RELIANCE is an
F&O stock (continuous trading ends 15:15, entry buffer 45 minutes). A `flat_by` of 15:08 is blocked: Zerodha squares off
F&O closing-auction stocks at 15:12, so 15:07 is the latest. The 15:12 sell that closes the position passes even though it is after the 14:45
entry cutoff: it is an exit.

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices, sec_list.csv or the broker log.
- Free Yahoo prices are unofficial and can lag; the broker's price is the one that fills, and a
  stop can fill worse than its price in a fast market. Circuit limits on exchange day can also be revised mid-day.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes. It never scrapes nseindia.com/api.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and (live
  mode) the user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
- No F&O, short selling or leverage beyond the broker's MIS unless the config allows it and the user confirms.
- Broker logins happen in the broker's own page, never in the chat.
