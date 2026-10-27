---
name: nse-broker
description: >-
  Order Executor for NSE/BSE, for the ten biggest Indian brokers (Groww,
  Zerodha, Angel One, ICICIdirect, Upstox, Kotak Neo, HDFC Sky, Dhan, SBI
  Securities, Motilal Oswal). Paper trading by default, never a real order
  without your yes. Shows each order as a plain ticket (entry, stop, target,
  max loss with charges, flat-by time), records your yes word for word, and
  in the default PAPER mode books a simulated fill in its own paper ledger
  (no Indian broker gives a paper account through its API) only when the
  market is at or through the limit, else keeps it as a working day order
  that fills or expires like a real one: stop and target
  tracked, P&L net of charges, in nse/state/broker.json and nse/portfolio.json.
  In LIVE mode it renders the order in the configured broker's own API field
  names (Kite place_order, Groww, SmartAPI, Breeze, Upstox, Neo, DhanHQ,
  Motilal) or as a manual ticket where there is no API, says honestly whether
  that broker's official MCP server can place orders (Groww yes; Zerodha's
  hosted one is read-only plus GTT; Upstox and Kotak read-only), and prints
  the SL-M stop or GTT OCO to place after the fill. Builds the sell ticket
  that closes an intraday position before flat_by, records fills, and logs
  each trade's max loss so nse-risk can hold the daily limit. Use when the
  user says "place the order", "buy it", "execute", "send it to my broker",
  "close my position", "square off", "flatten", "get me out", "connect
  Zerodha", "connect Groww", "set up Kite", "which broker", "paper trade",
  or after /nse-risk passes.
argument-hint: "[order number]"
---

# nse-broker

On Windows, use `py` in place of `python3`.

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every order:

1. **nse-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the order tool is set to "Needs approval". (Live mode only: the paper ledger sends nothing anywhere.)

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                      # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" working                   # paper: fill or expire working limit orders
python3 "${CLAUDE_SKILL_DIR}/ticket.py" track --price RELIANCE=1211.5   # paper: working orders, then stop / target
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE --product MIS
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
# live mode only:
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 118 --filled-price 1196.10
python3 "${CLAUDE_SKILL_DIR}/ticket.py" legs --index 1             # the SL-M stop / GTT OCO to place after the fill
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --product MIS --qty 118 --price 1211.30
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, keeps the paper ledger, builds the flatten ticket,
and keeps the log in `nse/state/broker.json`. `--now "2026-10-26 09:45"` pins the clock (India time, IST).

## What it reads and writes

| file | what |
| --- | --- |
| `nse/orders.json` | the proposed orders (nse-budget writes it; `flatten` replaces it) |
| `nse/state/risk.json` | nse-risk's verdicts; only a PASS from today is READY |
| `nse/state/broker.json` | every yes, no, placed, failed and filled, with each entry's max loss and each exit's net P&L |
| `nse/portfolio.json` | cash, positions per symbol and product (with margin, stop, target, flat_by) and `closed` trades with gross, charges, net |
| `nse/plan.json` | the flat-by time for `flatten` |
| `nse/prices.json` | a fresh quote (at most 15 minutes old) for a paper fill: is the limit marketable, the market-order price, `working` and `track` |

## Brokers: one profile each

`"broker"` in `nse/config.json` (default `"zerodha"`) picks a profile from `india_core.BROKERS`: `groww`, `zerodha`,
`angelone`, `icicidirect`, `upstox`, `kotak`, `hdfcsky`, `dhan`, `sbi`, `motilal`. An unknown name is refused by
every command. The profile holds the broker's brokerage and DP charge (nse-budget and nse-risk use them), its MIS
square-off times (flat_by is kept 5 minutes before the one for that stock and day) and how it holds a stop. `ticket.py`
adds one **ticket renderer** per broker: it turns the broker-neutral order in `nse/orders.json` into that broker's own
payload, with field names from `docs/brokers.md`. Every ticket says which fields are verified and which are not
(Angel One's enums, Kotak's field names, Motilal's intraday product and quantity field are unverified; HDFC Sky's fields
are unknown, so it prints a manual ticket). A value in `<angle brackets>` (an instrument token, an ISIN, an ICICI stock
code, a Dhan security id) is something this tool cannot know: look it up in that broker's instrument file. SBI Securities has
no public API: its ticket is one to type into the app. Check your own broker's current terms; none of this is advice.

| broker | order API (cost) | official MCP and orders | MIS stop | CNC stop |
| --- | --- | --- | --- | --- |
| Groww | Groww Trade API (paid plan) | **yes, places orders** (`mcp.groww.in/mcp`, OAuth) | SL_M order | GTT / OCO |
| Zerodha | Kite Connect | hosted `mcp.kite.trade/mcp` is **read-only plus GTT**; regular orders need a **self-hosted** server with your own Kite Connect key | SL-M order | GTT OCO |
| Angel One | SmartAPI (free) | none | stop-loss order | GTT |
| ICICIdirect | Breeze (free) | none; **the API refuses intraday (MIS) orders and market orders**, so MIS is a manual ticket | manual | GTT |
| Upstox | Upstox API | official MCP is **read-only** | SL-M order | GTT |
| Kotak Neo | Neo Trade API (free) | official MCP **cannot place orders** | SL-M order | GTT |
| HDFC Sky | Sky Open API | official MCP exists, **order placement not confirmed** | manual (fields unknown) | manual |
| Dhan | DhanHQ (trading APIs free) | none | STOP_LOSS_MARKET | Forever Order OCO |
| SBI Securities | none public | none | manual | manual |
| Motilal Oswal | MO API | none | STOPLOSS order (no SL-M) | GTT |

## Live mode per broker

Live mode is the user's own edit of `"paper": false`; never do it for them. Then for the configured broker:

- **Groww.** Connect the official MCP server: Customize > Connectors > Add custom connector, URL
  `https://mcp.groww.in/mcp`, log in with OAuth in the browser. It describes order tools: read the connected server's
  tool list, map the ticket's fields onto the order tool, set that tool to Needs approval. Selling needs DDPI on the account.
- **Zerodha.** Add `https://mcp.kite.trade/mcp` the same way (Claude Code: `claude mcp add --transport http kite
  https://mcp.kite.trade/mcp`) and log in on Zerodha's Kite page. The hosted server is read-only plus GTT: use it for
  holdings, positions, margins and a CNC GTT. A regular order (the MIS entry and its SL-M stop) needs a self-hosted
  kite-mcp-server with the user's own Kite Connect key and secret (github.com/zerodha/kite-mcp-server), typed into that
  server's own config, never the chat. Or place the ticket by hand in Kite.
- **Upstox, Kotak Neo.** Their official MCP servers are read-only: use them to read positions and orders, not to place.
  Place the ticket through the broker's official API (the user sets it up and holds the keys) or by hand in the app.
- **HDFC Sky.** `https://api.hdfcsky.com/mcp` exists; whether it places orders is not confirmed. Read its tool list; if it has
  no order tool, place the ticket by hand.
- **Angel One, ICICIdirect, Dhan, Motilal Oswal.** No official MCP server. Place the ticket by hand in the app, or through the
  official API if the user has set it up themselves. Community MCP servers exist: this pack does not recommend or use them.
- **SBI Securities.** No public API. Type the ticket into the SBI app.

Whatever the broker: **never invent a tool name or a parameter**. Tell Claude to read the connected server's tool list,
use only the names it shows, and map the ticket's fields onto them; if a name or field is unclear, show the user the
ticket and stop. Logins happen on the broker's own page. Credentials, TOTP codes and API keys never go in the chat; if the
user pastes one, tell them to delete the message and change it. Set every tool that places, changes, cancels or converts
anything to **Needs approval** (Claude Code: under `"permissions": {"ask": [...]}`, never `allow`); read-only tools can stay
on Always allow.

### SEBI retail-algo rules (in force since 1 April 2026)

These apply to any order **API**, which includes an MCP server that talks to one:
- **Static IP.** Order place, modify and cancel calls must come from a static IP registered with the broker against the API
  key (ICICI lets you change it once a week; Dhan, Upstox, HDFC Sky, Motilal check it). Data and read-only calls are exempt.
- **At most 10 orders per second** per exchange and segment on one key, without registering the strategy; above that the
  strategy must be registered with the exchange through the broker. This tool places a handful of orders a day.
- **Algo ID.** The broker tags API orders with an exchange Algo ID (a generic one below the threshold). Some APIs take a field
  for it (Upstox `X-Algo-Name` header, Motilal `algoid`): use the one the broker gave you.
- **A human approves every order.** That is this pack: nse-risk, the user's typed yes, then the broker's or Claude's own card.
- Daily login and token expiry are normal; brokers add their own limits (orders per day).
The framework text came from secondary sources; read the broker's own page and SEBI's circulars before relying on a detail.

## Paper mode (the default)

Zerodha has no paper trading, so the ledger is ours. A paper LIMIT behaves like a real day order. `approve` after a valid yes:

1. looks at the market: `--price P` (the last traded price, never the limit itself) or a `nse/prices.json` quote at most
   15 minutes old. If the limit is **marketable** (BUY: the market at or below the limit; SELL: at or above it), it books a
   simulated fill **at the limit price**;
2. otherwise (the market is away from the limit, or there is no fresh quote) it records the order as **WORKING** in
   `nse/state/broker.json`, with the yes word for word, the fingerprint and the time, and books nothing. Example: a BUY
   LIMIT at 3142.10 with the market at 3164.90 is working, not filled;
3. on a fill, takes the margin (MIS: value / leverage; CNC: the full value) and the buy-side charges out of the ledger cash,
   and keeps the stop, target and flat_by on the position, so `track` can follow them.

`ticket.py working` lists the working orders and checks each one; `track` does the same first. A working order fills at its
limit when the price trades through it: `--price SYM=P` or a fresh quote, else (unless `--offline`) the low (BUY) or high
(SELL) of Yahoo's 1-minute bars since the order time (5-minute bars if 1-minute fail; `--bars SYM=PATH` replays a saved chart
offline). It expires unfilled, with no ledger change, at the MIS entry cut-off (the end of continuous trading minus
`entry_buffer_min`, or `no_entries_after` if earlier: 14:30 for an F&O stock with the defaults until 2026-12-06), at the
broker's square-off for an MIS exit, and at the close for CNC. nse-risk counts a working order like a placed one until it
fills or expires. Show the user each WORKING, FILLED or EXPIRED line.

`track` then checks the open positions against `--price SYM=P`, a fresh quote in `nse/prices.json`, or (unless `--offline`)
Yahoo's 5-minute bars since the entry. A bar that touches both stop and target counts as the stop; a bar that
opens beyond the stop fills at the open, not at the stop. The exit is booked with the charges of both legs, so the
P&L you see is net of what the government and the broker take. It also lists MIS positions past flat_by or left over
from an earlier day. The stop in paper mode is the protective order you already approved, so its exit needs no new yes.
A real SL-M fills at the next market price: it can be worse than the ledger shows. Say so.

Going live is the user's own edit of `"paper": false`. Never do it for them.

## Live mode: what holds true at Indian brokers

- **No bracket orders in this pack.** An MIS trade is the entry, then a separate stop order after the fill (SL-M, or the
  broker's equivalent). Between the fill and the stop being placed the position is unprotected: place the stop first, before anything else.
- The entry is a `LIMIT` order. It may not fill, or fill partly: the stop quantity must be the quantity that actually filled
  (`ticket.py legs` uses the recorded fill, or `--qty`).
- A target as a second `LIMIT` sell is optional and risky for MIS: there is no OCO. If the stop triggers first, the target order
  is still open and could later fill and leave you SHORT. Cancel the other order the moment one fills, or use an nse-alerts
  target alert instead and flatten by hand.
- **CNC delivery** protects with a GTT / OCO placed after the entry fills (a Forever Order at Dhan). A GTT trigger sends a LIMIT
  order, which can stay unfilled in a gap past the stop. Only Zerodha's GTT payload is shown as JSON; for the others the ticket
  says what to set and the user types it in the app (their GTT field names are not verified here).
- **MIS is auto-squared-off** by every broker (with a fee), earlier for F&O stocks until 2026-12-06 (the closing auction, which does
  not accept stop-loss orders). flat_by is set 5 minutes before the square-off for that stock; nse-risk enforces it.
- Delivery sells use up the DP charge and settle T+1; shares bought today cannot be sold as CNC until they arrive.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/nse-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all". The yes covers the entry and the protective stop shown on the same ticket.
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "yes to all", or a yes that names a different
   stock or amount). Ask again. Never turn a reply into a yes yourself.
4. **Paper**: refresh `nse/prices.json` (nse-alerts) first, or pass `--price P` with the last traded price. `approve` then
   prints "PAPER FILL" (the limit was marketable: show it and the tracked stop and target) or "PAPER WORKING ORDER" (show
   why it did not fill and when it expires; then run `ticket.py working` as prices move).
   **Live**: only after "Recorded your yes", send exactly the payload that was printed through the route the ticket named (the broker's
   MCP server tool, found in its tool list, or by hand). If Claude's card appears: Allow once or Deny.
5. **Live**: record what happened: `ticket.py result --index N --status placed --broker-id <id> --filled-qty Q
   --filled-price P` (from the response or the positions tool), or `--status denied`, or `--status failed` with
   `--note`. A failed order is not retried without a new check and a new yes. Then `ticket.py legs --index N` prints the
   stop (MIS) or GTT OCO (CNC) to place; place it at once, through the same card.

## Steps: while it is open, and out before flat_by

- nse-alerts watches the stop, the target and the flat-by time (`alerts.py add --from-plan`). In paper mode run `ticket.py track` as prices move
  (it also fills or expires a working entry).
- When the stop or target fills in live mode, record it: `ticket.py fill --symbol RELIANCE --side sell --product MIS --qty 118 --price 1211.30`.
- By `flat_by` (15:07 IST for an F&O stock at Zerodha until 2026-12-06, else 15:10), anything still open gets closed:
  1. Ask the broker how many shares are held (its positions tool or app, read only). In paper mode the ledger knows.
  2. `ticket.py flatten --symbol RELIANCE --product MIS` writes a sell for those shares to `nse/orders.json`
     (MARKET, or `--limit-price P` for a LIMIT if the broker's API insists on a price).
  3. `/nse-risk`. A sell that only closes what the user holds is never blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one order, as above. In paper mode a market exit needs `--price P` (or a fresh quote).
  5. **Live**: cancel the open protective stop first (the server's cancel tool, Needs approval), or both orders could execute and
     leave a short position. For CNC, delete the GTT. Then send the sell ticket, then `result`.

## Example: the reel (paper)

```
ORDER 1 of 1, PAPER ledger, no real money, Zerodha
  BUY 118 RELIANCE (MIS intraday), LIMIT 1196.10, valid today
  stop-loss SL-M trigger 1188.50 (a SELL placed after the fill)
  target 1211.30
  max loss about ₹993.82 if the stop fills at 1188.50, charges included (it can fill worse in a fast market)
  flat by 15:07 IST
  charges (government and broker): about ₹97.02 round trip at the stop, ₹97.78 at the target
  READY: nse-risk PASS, waiting for your yes
```

The user replies "yes" with RELIANCE at 1195.00. `approve` prints `PAPER FILL (simulated, nothing sent to Zerodha): BUY 118
RELIANCE at ₹1,196.10 (your limit price; the market was 1195.00, ...)`. Had the market been at 1199.40, it would print
`PAPER WORKING ORDER ... is NOT filled` and fill later only when a 1-minute bar's low reached 1196.10 before 14:30.
At 11:30 `track --price RELIANCE=1212` books the target: +₹1,793.60 before charges, +₹1,695.81 net after all charges.
In live mode the same yes prints the entry JSON for Kite's `place_order`:
`{"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY", "quantity": 118,
"product": "MIS", "order_type": "LIMIT", "price": 1196.1, "validity": "DAY"}`; the `why` and the plan stay in the
file and are never sent.

## What it never does

- Never places an order without all gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats passwords, 2FA codes or API keys.
- Never switches `"paper"` to live. The user does that themselves.
- Never calls cancel-all, position-conversion or any order tool on its own initiative; each needs the user and the card.
- Never invents a tool name, a parameter or a field: the connected server's tool list and the ticket's notes are the only sources.
- Never promises a fill price: a limit may not fill, and a market order and a stop fill at whatever the market gives.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and (live
  mode) the user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
- No F&O, short selling or leverage beyond the broker's MIS unless the config allows it and the user confirms.
- Broker logins happen in the broker's own page, never in the chat.
