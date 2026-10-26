---
name: nse-timemachine
description: >-
  Time Machine for NSE/BSE trades. Replays every possible outcome of a trade
  before you risk a rupee: 10,000 simulated sessions plus every past time the
  setup fired. Takes the plan from nse-exit (entry, stop, target, MIS or CNC),
  rebuilds the rest of the day 10,000 times from the stock's own 5-minute
  bars over the last 60 days (RELIANCE, TCS, INFY, HDFCBANK, any .NS or .BO
  ticker), and re-runs the same rule on every past session. Results are in R
  with the round-trip charges (STT, brokerage, exchange, SEBI, stamp, GST)
  already inside, so a 1R target nets less than 1R. Prints the odds of target
  first, stop first, or neither by the flat-by exit (default 15:10 IST), where
  the price sits then, the average R, the history, and one honest verdict,
  then hands off to /nse-budget. Use when the user says "run the time
  machine", "replay this trade", "what are the odds", "simulate this trade",
  "monte carlo", "backtest this setup", "how often does this hit the target",
  "is this a coin flip", "will charges eat this", or after /nse-exit writes a
  plan, before /nse-budget.
argument-hint: "[path to plan.json, or TICKER]"
---

# nse-timemachine

On Windows, use `py` in place of `python3`.

The Time Machine sits between the plan and the money. nse-exit has set the
entry, the stop and the target. Before anything is sized or sent, this skill
asks one question: if today plays out like the stock's own last 60 days, how
often does this plan hit the target first, the stop first, or neither? It
answers with 10,000 replays and with the real record of the same rule on
every past session. It is a simulation built from the stock's own recent
bars, not a prediction. Day trading loses most people money, and a
good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # nse/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save nse/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:00      # end each replay earlier than the plan's flat_by
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-10-26 \
    --entry 1193.60 --stop 1183.65 --target 1203.55 --entry-time 10:00
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--product MIS|CNC` (flags mode), `--qty N` (shares, for the charges),
`--config PATH` (default `nse/config.json`), `--offline FILE` (a saved Yahoo
chart JSON, `.json` or `.json.gz`), `--now "2026-10-26 10:30"` (IST) to pin
the clock.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/plan.json` | the plan from nse-exit: symbol, yahoo, exchange, product (MIS/CNC), date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range, flat_by. A missing product, exchange or flat_by defaults to MIS, NSE and the config's `flat_by` (else 15:10). A `quantity` in the plan sizes the charges |
| `nse/config.json` | optional: `flat_by`, and `charges` overrides for your broker |
| Yahoo chart API | one request: 60 days of 5-minute bars (`range=60d&interval=5m`) for `RELIANCE.NS` / `RELIANCE.BO`, free and unofficial, can lag |
| `nse/timemachine.json` | with `--write`: `{plan, costs, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the exit price |

## How it works

1. **The pool.** It keeps only FULL sessions before the plan's date: 72 usable
   five-minute bars, 09:15 to 15:15 IST. Yahoo's NSE 5-minute data does not
   carry the 15:20 and 15:25 bars (null on every day): since 2026-08-03 F&O
   stocks stop continuous trading at 15:15 and close through an auction (until
   2026-12-06; from 2026-12-07 continuous trading runs to 15:30 again). So
   replays end by 15:15, which is why this works with the 15:10 flat-by and not
   later. The forced exit is the earlier of the plan's flat-by and the end of
   continuous trading for that stock (`cas_stock` in the plan, else looked up
   in NSE's `fo_mktlots.csv`, cached in `nse/state/fo_mktlots-YYYY-MM-DD.csv`;
   a cache up to 7 days old is reused if the fetch fails; if the list cannot be
   loaded the stock is treated as F&O, the cautious choice, and a warning says
   so; `--fo-lots FILE` gives it by hand). Warnings also name your broker's
   MIS square-off time (`"broker"` in `nse/config.json`, default zerodha).
   Muhurat trading (Sunday 2026-11-08, 14:20 to 15:30) is refused: a one-hour
   session does not fit the 09:15 grid. Muhurat
   or other short sessions, gaps and today are left out. Every bar becomes four
   ratios to the bar before it: open, high, low and close over the previous close.
2. **One replay.** Start at the entry price. For each bar left until the exit,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **The exit.** MIS: the plan's `flat_by` (default 15:10). The position is
   squared off there, win or lose, so "neither" means "still open at 15:10 and
   sold". CNC (delivery): there is no forced exit, but **the intraday replay
   does not model overnight holding**. It only simulates today, until the last
   bar Yahoo carries (15:15, the market closes 15:30). Gaps, news and the
   moves of the days after, and T+1 settlement, are not in a CNC run. Say that
   plainly whenever the product is CNC.
4. **R includes charges.** 1R is the max loss of the trade: `(entry - stop) x
   qty` plus the round-trip charges if the stop is hit. A target nets
   `(target - entry) x qty` minus its charges, so a "1R" target is worth about
   +0.8R on a small trade (charges are a fixed rupee amount plus percentages, so
   a tight stop hurts most). The stop is exactly -1R. Anything else is the net
   P&L at the exit over that 1R. The charges come from `trade_charges()`
   (Zerodha defaults, overridable in `nse/config.json`); the run prints them
   as their own line. The quantity is the plan's, or a reference position of
   about ₹1,00,000 when the plan has none (it says which; a plan from nse-exit carries `charges.qty`, which is used and labelled "reference position from the plan"): nse-budget sizes
   the real one, and the real charges can differ a little.
5. **Four odds.** Target first. Stop first. Neither. Green, meaning finished
   in profit after charges. A gap through the stop still counts as -1R. A real
   fill (SL-M order) can be worse.
6. **The history.** The plan's own rule (the same opening range, stop rule
   and target R, prices on the 0.05 tick) runs again on every one of those past
   sessions, with the same charges. That is what really happened, not a
   simulation.
7. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first
   against what the target's NET R needs: 1 in (1 + R). Less than 5 points
   from break-even: "Close to a coin flip" for a 1R plan, "Close to
   break-even" otherwise. 5 to 15 points: "Leans toward the target" or "Leans
   toward the stop". 15 or more: "Strongly toward" one of them. When charges
   shave the target (always, in practice) the line says so: "after charges the
   1R target is worth 0.82R, so it has to come first in 55 %".
8. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither, and the average R when its sign goes against the lean.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 full sessions, when the entry bar is
not in the data (wrong date, not closed yet, a future day), when the plan's
day is a holiday, a weekend or a Muhurat/special session (its hours do not fit
the 09:15 grid), or when the plan does not make sense (a stop above a buy's
entry, an entry off the 5-minute grid, a CNC short). A live in-progress bar is
never used.

## Steps

1. Make sure `nse/plan.json` is today's plan from `/nse-exit`. If there is
   none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. The run already ends at the plan's
   flat-by time for MIS.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the exit, the average R, the charges line (what 1R
   is in rupees and what the target actually nets), the history line, then the
   verdict line and any line under it exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   Results, news, circuits and halts can do things those 60 days never did.
   Check `/nse-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/nse-budget` to size it.

## Example

RELIANCE, Monday 2026-10-26, MIS. The 15-minute opening range is 1175.30 to
1192.00. The 09:55 bar closes above it: entry ₹1,193.60 at 10:00 (bar 8),
stop ₹1,183.65 (the range midpoint, on the tick), target ₹1,203.55 (1R, risk
₹9.95 a share), flat by 15:10.

```
10,000 replays of the rest of the session, 10:00 to 15:10 (62 five-minute bars, the plan's flat-by exit), seed 19.
  target first   23.3 %
  stop first     30.5 %
  neither        46.2 %   still open at 15:10 (squared off there)
  green          42.4 %   finished in profit after charges
  Price at 15:10: median ₹1,192.80, 90 % of replays between ₹1,178.41 and ₹1,207.71.
  Average R: -0.162 (net of charges).
R and charges (83 shares, a reference position of about ₹1,00,000): 1R = ₹907.97 = ₹825.85 risk + ₹82.12 charges.
The target nets ₹743.26 after ₹82.59 charges = +0.82R (a 1R target before charges).
History: the same rule on each of those 58 real sessions fired 26 times: 13 target, 10 stop, 3 neither. Average R -0.127.
Leans toward the stop: after charges the 1R target is worth 0.82R, so it has to come first in 55 % of the replays that hit one side; it did in 43 %.
```

That is the honest answer for this setup, and it is what the user should hear
before `/nse-budget` sizes it: the government and the broker take about ₹82
per round trip here, which is 9 % of the rupees at risk.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's net R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- For a CNC plan, always say the replay is intraday only.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
  Never set an order tool to "Always allow".
- Intraday (MIS) trades must be closed the same day; the broker squares off open MIS positions around
  the square-off time of your broker (Zerodha: 15:12 for F&O stocks, 15:25 for others) with a fee. Leverage and short selling are the user's call and v1 plans are longs only.
- Broker keys and logins stay in the browser or the broker's own setup, never in the chat.
