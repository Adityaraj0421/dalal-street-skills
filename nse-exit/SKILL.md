---
name: nse-exit
description: >-
  Exit Planner for NSE/BSE. Sets your stop and your target before you enter,
  so you know when to get out. Reads the entry from nse-entry (or a price you
  give), puts the stop at the opening-range midpoint by default (or the range
  low, your price, a percent, or a multiple of the ATR), rounds it down to the
  0.05 tick, and sets the target at 2R (or 1R, or your price). Shows the
  round-trip charges (brokerage, STT, GST, stamp duty) and reward:risk after
  them. Refuses a stop at or above the entry, a target at or below it, or a
  stop too tight to survive the spread, and says why. Carries the product (MIS
  intraday or CNC delivery) and the flat-by time into nse/plan.json for the
  Time Machine, the Budget Manager and Price Alerts. Long side only. Use when
  the user says "where's my stop", "set my stop and target", "plan the exit",
  "when do I get out", "stop loss", "what's my R", "risk reward", "RELIANCE
  stop loss in rupees", or right after /nse-entry triggers, before
  /nse-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X] [--product MIS|CNC]"
---

# nse-exit

On Windows, use `py` in place of `python3`.

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap, a circuit limit or a fast market can fill past it. Day trading loses most
people money, and a neat plan does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # nse/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1.5R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --entry 1193.60 --or-high 1192 --or-low 1175.30
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol TCS --entry 2530 --stop pct:1 --product CNC --qty 10
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current nse/plan.json
```

Other flags: `--flat-by 15:10`, `--json`, `--now "2026-10-26 10:00"` (India
time, IST), `--entry PRICE` with a waiting entry.json to plan ahead, `--setup
level`, `--offline FILE` (a saved Yahoo chart JSON for an ATR stop), `--qty N`
(shares, for the charges line).

## What it reads and writes

| file | what |
| --- | --- |
| `nse/entry.json` | the trigger from nse-entry: symbol, exchange, yahoo, product, entry price, time, bar, opening range |
| `nse/config.json` | `flat_by` (15:10), `min_reward_risk` (1.0: a note here, nse-risk enforces it), `max_loss_per_trade_inr` (sizes the charges line), `charges` (overrides of the default Zerodha charges) |
| `nse/holidays.json` | optional calendar additions (holidays, special sessions) |
| Yahoo chart API | only for an `atr:K` stop: the session's bars up to the entry bar, free and unofficial |
| `nse/plan.json` | with `--write`: the plan below. nse-timemachine, nse-budget and nse-alerts read it |

## nse/plan.json (the exact shape)

```json
{
  "created_by": "nse-exit",
  "symbol": "RELIANCE", "exchange": "NSE", "yahoo": "RELIANCE.NS",
  "product": "MIS",
  "side": "buy", "date": "2026-10-26", "setup": "orb",
  "entry": 1193.6, "entry_time": "10:00 AM", "entry_bar": 8, "interval": "5m",
  "opening_range": {"high": 1192.0, "low": 1175.3, "minutes": 15},
  "stop": 1183.65, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
  "target": 1208.5, "target_r": 1.5, "target_label": "1.5R (1.5 times the risk)",
  "risk_per_share": 9.95, "reward_risk": 1.5, "reward_risk_net": 1.29,
  "flat_by": "15:07", "cas_stock": true, "squareoff": "15:12", "broker": "zerodha",
  "charges": {"qty": 100, "at_stop": 89.27, "at_target": 89.97,
              "breakdown_at_stop": {"brokerage": 40.0, "stt": 29.59, "exchange_txn": 7.3, "sebi": 0.24,
                                    "stamp_duty": 3.58, "gst": 8.56, "dp_charge": 0.0, "total": 89.27},
              "breakdown_at_target": {"...": "same keys"},
              "net_reward": 1400.03, "net_risk": 1084.27, "reward_risk_net": 1.29,
              "basis": "a reference position (nse-budget sizes the real one)", "note": "..."},
  "stop_detail": "(₹1,192.00 + ₹1,175.30) / 2 = ₹1,183.65",
  "created_at": "2026-10-26T04:31:00Z",
  "note": "A stop is a plan, not a guarantee: ...",
  "atr": 3.2182, "atr_bars": 9
}
```

- It is the US plan.json shape (same field names) plus `exchange`, `yahoo`,
  `product`, `reward_risk_net` and `charges`. `atr` and `atr_bars` appear only
  for an `atr:K` stop. `opening_range` is null when no range was given.
- `symbol` is the trading symbol (`RELIANCE`), `exchange` is `NSE` or `BSE`,
  `yahoo` is the chart symbol (`RELIANCE.NS`). `product` is `MIS` or `CNC`.
- Prices are rupees as plain numbers, all on the 0.05 tick. `risk_per_share`
  is entry minus stop. `reward_risk` is price geometry only; `reward_risk_net`
  takes the charges off the reward and adds them to the risk.
- `flat_by` is IST `HH:MM`, always present. For MIS it is the time you must be
  out. For CNC (delivery) it is only where an intraday replay stops.
- `cas_stock` is true for an F&O stock (closing auction: see Flat by below),
  `squareoff` is your broker's MIS auto square-off time for it (null for CNC),
  `broker` is the broker key from `nse/config.json`.
- `charges` is a round trip for `qty` shares (buy at the entry, sell at the
  stop or at the target). Downstream skills size the real quantity and
  recompute with the same `trade_charges`; use these numbers for display.
  `entry_time` is the trigger bar's close, `"10:00 AM"`, or null when planned
  ahead.

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **Rounding (tick 0.05).** The entry rounds UP, the stop rounds DOWN (away
  from the entry) and the target rounds DOWN, all to the 0.05 tick. Risk =
  entry minus stop. Target = entry + R x risk. `2R` by default; `1R`, `1.5R`
  or `price:X` also work.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under 0.05% of the price or two ticks (Rs 0.10): too tight
  to survive the spread, normal wiggle would stop you out. An entry.json that
  has not triggered (unless you give `--entry`). A short: v1 plans longs only.
  An index. A `flat_by` you set (flag or config) for MIS at or after your
  broker's square-off time: the broker squares off open MIS positions then and
  charges a fee.
- **Flat by.** 15:10 by default (or `flat_by` in the config), but never later
  than 5 minutes before your broker's MIS square-off for that stock. Out by
  then, win or lose, no overnight hold (MIS). The plan prints the square-off
  time and any note (an unverified time, or one read before the December
  change). An MIS entry at or after the flat-by time is refused.
- **Broker.** `"broker": "zerodha"` in `nse/config.json` (default) picks the
  brokerage and the square-off times of one of ten brokers (groww, zerodha,
  angelone, icicidirect, upstox, kotak, hdfcsky, dhan, sbi, motilal). Zerodha
  squares off F&O stocks at 15:12 (flat by 15:07), Dhan at 15:10 (15:05).
  Override with `"squareoff": {"cas": "15:05", "other": "15:20"}`.
- **Closing auction.** From 2026-08-03 to 2026-12-06 F&O stocks stop
  continuous trading at 15:15 and the auction takes no stop-loss orders, so
  the plan is built to be out before it. From 2026-12-07 every stock trades to
  15:30 again. The F&O list is NSE's open `fo_mktlots.csv`, cached in
  `nse/state/fo_mktlots-YYYY-MM-DD.csv` (a cache up to 7 days old is reused if
  the fetch fails, with a note). `--fo-lots FILE` gives it by hand.
- **Muhurat.** The built-in 2026 session is Sunday 2026-11-08, 14:20 to 15:30:
  flat by is 5 minutes before the end of continuous trading (15:25, or 15:10
  for an F&O stock).
- **Charges.** Zerodha's published rates by default (overridable in the config
  `charges`): brokerage, STT, exchange, SEBI, stamp duty, GST, plus the DP
  charge on a CNC sale. They are their own line, and the Reward:risk line shows
  the ratio before and after them. Small targets lose a lot of their reward to
  charges: say so when reward:risk after charges is thin.

## Steps

1. Run it right after `/nse-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk (before and after
   charges), the charges and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/nse-timemachine` to replay the plan, then `/nse-budget`
   to size it to the max loss per trade. Offer `/nse-alerts` for the stop,
   the target and the flat-by time. Claude can keep `entry.py watch` running
   in the background or on a `/loop` until a setup triggers, then come here.

## Example: the reel

RELIANCE, Monday 2026-10-26. The entry is 1193.60 at 10:00 AM (the 9:55 bar,
bar 8) and the opening range is 1175.30 to 1192.00. `exit.py plan --stop
or-mid --target 1.5R --write`:

```
Exit plan: RELIANCE long (NSE MIS), Mon 2026-10-26, opening-range breakout on 5-minute bars
  Entry         ₹1,193.60 at 10:00 AM (bar 8)
  Stop          ₹1,183.65  opening-range midpoint: (₹1,192.00 + ₹1,175.30) / 2 = ₹1,183.65
  Target        ₹1,208.50  1.5R (1.5 times the risk)
  Risk a share  ₹9.95 (0.83% of the entry)
  Reward:risk   1.5 before charges, 1.29 after
  Charges       ₹89.27 at the stop, ₹89.97 at the target (100 shares, MIS round trip: brokerage, STT, exchange, SEBI, stamp duty, GST)
  Flat by       15:07 IST, win or lose: intraday, no overnight hold
  Square-off    15:12 IST: Zerodha auto-squares-off open MIS F&O positions then, with a fee
```

The default 2R target would be 1193.60 + 2 x 9.95 = 1213.50. nse-budget then
sizes it: a Rs 500 max loss per trade (charges included) buys fewer than
floor(500 / 9.95) = 50 shares.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk,
  charges. It never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Long side only. v1 plans longs only.
- The price band (circuit limit) is not checked here: `/nse-risk` checks the
  entry, stop and target against it.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`nse-broker` keeps a built-in paper ledger). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, futures, short selling or leverage beyond MIS unless the config allows it and the user confirms.
- Broker keys and logins stay in the broker's own login or the Claude Desktop config, never in the chat.
