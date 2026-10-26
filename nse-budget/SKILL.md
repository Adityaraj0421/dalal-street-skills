---
name: nse-budget
description: >-
  Budget Manager for NSE/BSE. Sizes every position to your capital and your
  max loss per trade, charges included. Reads the trade plan from nse-exit
  (entry, stop, target, MIS or CNC), finds the whole shares that fit inside
  four limits (max loss at the stop INCLUDING round-trip STT, brokerage, GST
  and stamp duty; position cap; max order; cash or MIS margin after your
  buffer), says which limit decided it, and writes the order set to
  nse/orders.json: an entry LIMIT plus an SL-M stop for MIS intraday, or an
  entry plus a GTT OCO stop/target for CNC delivery. A stop too far for your
  max loss is a plain refusal, not a smaller order. Also splits a CNC budget
  (for example Rs 5 lakh) across Nifty names by weight. Nothing is placed.
  Use when the user says "how many shares", "size this trade", "how much
  should I buy", "I have 2 lakh", "I only want to risk Rs 1,000 a trade",
  "set my rules", "keep 10% in cash", "how much margin", "allocate my
  portfolio across Nifty stocks", or after /nse-exit writes a plan for
  RELIANCE, TCS, HDFCBANK or any NSE stock.
argument-hint: "[budget in rupees]"
---

# nse-budget

On Windows, use `py` in place of `python3`.

The Budget Manager turns a plan ("buy RELIANCE at 1196.10, stop 1188.50")
into an exact order ("buy 118 shares"), inside rules the user sets once. The
stop decides the size: the further away the stop, the fewer shares. It writes
a proposal only. nse-risk checks it and nse-broker asks the user yes or no.
Most people who trade intraday lose money. Sizing to a max loss keeps each
loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                         # nse/plan.json + nse/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                 # write nse/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --product CNC --write   # a delivery order with a GTT OCO exit
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1196.10 --json  # a limit order at 1196.10
python3 "${CLAUDE_SKILL_DIR}/size.py" --targets "${CLAUDE_SKILL_DIR}/targets.example.json" --budget 500000 --prices nse/prices.json   # allocation mode, below
```

`--now "2026-10-26 09:45"` pins the clock (India time, IST). A plan for
another day is refused: make a new one with nse-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/config.json` | the rules, in rupees (start from `config.example.json` in this folder) |
| `nse/plan.json` | the trade: symbol, entry, stop, target, product, flat_by (nse-exit writes it) |
| `nse/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (nse-alerts writes it) |
| `nse/portfolio.json` | optional: its cash caps the size (nse-broker keeps it) |
| `nse/orders.json` | the proposed order set it writes with `--write` |

## Step 1: set the rules once

If `nse/config.json` does not exist, copy `config.example.json` into
`nse/config.json` and ask the user for each value in plain words:

- **budget**: the capital this is for (example Rs 2,00,000). Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills at the stop, round-trip charges included (default Rs 1,000).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the most capital one stock may tie up (50% of the budget), the biggest order value (Rs 4,00,000) and the smallest (Rs 500).
- **mis_leverage** (default 5): MIS needs value / leverage as margin. Under SEBI's peak-margin rules the real
  margin varies by stock: tell the user to check their broker's margin calculator and set a lower number if it is stricter.
- The day limits nse-risk enforces: **max_daily_loss_inr** (3,000), **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:30" / "14:45"), **entry_buffer_min** (45: no MIS entry in the last 45 minutes of continuous trading) and
  **flat_by** ("15:10"), all India time. nse-budget never lets flat_by run past the broker's MIS square-off:
  the order's flat_by is the earliest of your flat_by, the plan's flat_by and **5 minutes before that broker's
  square-off for that stock on that day** (`default_flat_by()`).
- **blocked**: symbols they never want to trade.
- **broker** (default `"zerodha"`): one of `groww`, `zerodha`, `angelone`, `icicidirect`, `upstox`, `kotak`,
  `hdfcsky`, `dhan`, `sbi`, `motilal` (an unknown name is refused). It sets the brokerage, the DP charge and the MIS
  auto square-off time, all from `india_core.BROKERS`. **charges** overrides any single number (for example a
  discounted brokerage); **squareoff** (`{"cas": "15:12", "other": "15:25"}`) overrides the broker's square-off times.
  If a broker's square-off time is unverified, or was read before SEBI's 2026-12-07 auction change, the report prints a note: tell the user to check it.
- **Closing auction stocks.** Until 2026-12-06, F&O stocks stop continuous trading at 15:15 and brokers square off
  their MIS positions earlier (Zerodha 15:12, others 15:00-15:10). nse-budget reads NSE's open `fo_mktlots.csv`
  (cached as `nse/state/fo_mktlots-YYYY-MM-DD.csv`, a cache up to 7 days old is reused if the download fails, and it says
  so) to know which stocks those are. With no list at all every stock counts as one, which gives the earlier flat_by.
  From 2026-12-07 every stock trades to 15:30 again. `--offline --fo-lots FILE` uses a local file and no network.
- Keep **paper** true until they have watched it work in the paper ledger.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only. The size is the smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | the most shares whose (entry minus stop) x shares PLUS the round-trip charges at the stop fits `max_loss_per_trade_inr` |
| position cap | budget x `max_position_pct` / margin a share (MIS: price / leverage, CNC: price) |
| max order | `max_order_inr` / price |
| cash | (budget minus the cash buffer) / margin a share, never more than the cash in `nse/portfolio.json` |

The entry is a LIMIT order at the plan's entry rounded UP to the 0.05 tick, or
at the price in `nse/prices.json` if it is fresh (under 15 minutes old) and
higher: a price that ran up since the plan means a bigger loss at the same
stop. A lower price never shrinks the risk. The stop and target round DOWN to
a tick. `--limit PRICE` sets the limit yourself.

**Charges are their own line.** Show the user what the government and the broker
take: brokerage, STT, exchange fee, SEBI fee, stamp duty, GST, and for CNC the
DP charge on the sale. Reward:risk is computed net of charges on both outcomes,
so a tight trade can have a plan R of 2.0 and a real one of 1.7.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, net reward:risk is under `min_reward_risk`, the stop is not
below the entry, the target is not above it (the move already happened), the
plan is for another day, the symbol is blocked, the series is BE/BZ with MIS
(trade-for-trade: delivery only), the symbol is an index, or the plan is a
short. Say the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the order value,
the margin, the max loss at the stop (gross + charges) and the charges line.
Say plainly that a stop is an order, not a promise: in a fast market it can
fill below the stop. Then hand over to `/nse-risk`.

## The order file

`nse/orders.json` is broker-neutral at the top of each order (`side`, `qty`,
`product`, `order_type`, `price`, `stop_trigger`, `target_price`, `broker`) and also carries the
Kite-shaped legs, which are the `zerodha` rendering of those fields:

```json
{"id": "RELIANCE-20261026-0945", "symbol": "RELIANCE", "exchange": "NSE", "yahoo": "RELIANCE.NS", "series": "EQ",
 "product": "MIS", "side": "BUY", "qty": 118, "order_type": "LIMIT", "price": 1196.1,
 "stop_trigger": 1188.5, "target_price": 1211.3, "broker": "zerodha",
 "entry": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
           "quantity": 118, "product": "MIS", "order_type": "LIMIT", "price": 1196.1, "validity": "DAY"},
 "stop": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
          "quantity": 118, "product": "MIS", "order_type": "SL-M", "trigger_price": 1188.5, "validity": "DAY"},
 "target": {"price": 1211.3, "how": "LIMIT sell placed after the fill (optional, see nse-broker), or an nse-alerts target alert"},
 "gtt_oco": null,
 "max_loss": 993.82, "charges_at_stop": 97.02, "charges_at_target": 97.78, "reward_risk": 1.71,
 "margin_needed": 28227.96, "flat_by": "15:07", "fingerprint": "..."}
```

For CNC, `stop` is null and `gtt_oco` holds the two-leg GTT (`trigger_values` [stop, target]).
`why` and `plan` are notes for nse-risk and nse-broker; the broker sees only the legs. The
`fingerprint` identifies the exact order: change a quantity or a price and nse-risk must check it again.

## Example: the reel

RELIANCE on Monday 2026-10-26. Opening-range breakout: entry 1196.10 at 09:45,
stop 1188.50 (the opening-range midpoint), target 1211.30 (2R). Risk 7.60 a
share. The example config: Rs 2,00,000 budget, 10% buffer, Rs 1,000 max loss, 5x MIS.

| limit | the math | shares |
| --- | --- | --- |
| max loss | Rs 1,000 incl. charges at 7.60 a share | **118 (decides it)** |
| position cap | 50% of Rs 2,00,000 = Rs 1,00,000 / 239.22 margin | 418 |
| max order | Rs 4,00,000 / 1196.10 | 334 |
| cash | Rs 1,80,000 / 239.22 margin | 752 |

RELIANCE is an F&O stock, so with Zerodha (closing-auction stocks square off at 15:12) the order carries `flat_by` 15:07 until 2026-12-06.
118 shares = Rs 1,41,139.80 order value, Rs 28,227.96 margin. Max loss at the
stop: 118 x 7.60 = Rs 896.80 plus charges Rs 97.02 = Rs 993.82. Without charges
131 shares would have "fit"; the charges took 13 of them away. Net reward:risk
is 1.71, not 2.0. With the stop at 100.00 instead, one share would risk 1,096:
no order.

## Allocation mode (CNC portfolios)

`--weights RELIANCE=25,TCS=20` or `--targets FILE` (`targets.example.json`
here) splits a budget across a list by weight, in rupees and whole shares (NSE has no fractional
shares): the cash buffer comes off the top, weights are renormalised to 100, any
stock over `max_position_pct` is pinned at the cap and its excess shared out,
slices under `min_order_inr` are dropped, then each slice is floored to whole shares and the
leftover goes one share at a time to the stock furthest below its slice. It needs prices
(`--prices nse/prices.json`; nse-alerts: `alerts.py quote RELIANCE TCS --write`). The orders are CNC
LIMIT buys with no stop, and the report shows the buy-side charges and the money left idle. nse-risk
warns about the missing stop (it only blocks if `require_stop_cnc` is true) and applies your size,
position and cash limits to every buy. For a Rs 5 lakh portfolio use a separate config (`--config`).

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag. The broker's price is the one that fills.
- Charges use the configured broker's brokerage and DP charge (Zerodha MIS Rs 20 cap, DP Rs 15.34; Groww, Angel One, Upstox
  and others differ: see `india_core.BROKERS`) with the shared statutory rates. Some are unverified; override in `charges` if your contract note differs.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes options, futures or short positions.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and (live
  mode) the user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
- No F&O, short selling or leverage beyond the broker's MIS unless the config allows it and the user confirms.
- Broker logins happen in the broker's own page, never in the chat.
