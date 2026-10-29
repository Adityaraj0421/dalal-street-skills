# Indian Retail Stockbrokers: Per-Broker Facts for Trading Tools

Research date: 2026-10-26. Compiled with web search and fetches of official pages. No NSE/BSE APIs were scraped.

**Confidence legend**
- **[V]**: verified on an official broker, exchange or regulator page (URL given).
- **[S]**: secondary source only (news, aggregator, forum, community docs), or the official page could not be rendered.
- **[U]**: unknown. Not found, or the sources conflict too much to pick one.

**Read this first.** Two market changes in 2026 make many older web pages wrong:
1. **NSE Closing Auction Session (CAS), from 3 Aug 2026.** F&O-eligible stocks in the cash market now close via an auction from 3:15 to 3:35 PM. Brokers moved MIS auto square-off **earlier for CAS stocks**, typically 3:00 to 3:12 PM. Pages dated before August 2026 show the old times. [S] https://fundsindia.com/blog/investor-key-notes/introduction-of-closing-auction-session/35062 , https://www.share.market/support/home/trading-investment/closing-auction-session-cas/what-will-be-the-new-mis-auto-square-off-timings/
2. **SEBI retail-algo framework, fully in force from 1 Apr 2026.** It requires a static IP for order APIs, OAuth/2FA, a daily session, a cap of 10 orders per second, and Algo IDs. See the SEBI section below.

---

## 1. Ranking by NSE active clients (September 2026)

**Source:** NSE broker-wise active clients for **September 2026**. An NSE "active client" traded at least once in the trailing 12 months (1 Oct 2025 to 30 Sep 2026). Exchange-wide total: 4,61,20,857.
- Exact figures from the NSE workbook as mirrored by OneTradeJournal: https://onetradejournal.com/brokers/active-clients-by-broker [S]
- Cross-checked against Inc42 (14 Oct 2026): https://inc42.com/buzz/brokers-in-september-groww-strengthens-lead-angel-one-closes-in-on-zerodha/ [S]
- Also https://www.cxodigitalpulse.com/?p=63189 [S]

| Rank | Broker (NSE member name) | Active clients, Sep 2026 | Share | MoM change |
|---|---|---|---|---|
| 1 | Groww (Groww Invest Tech) | 1,34,48,194 | 29.16% | +1,00,961 |
| 2 | Zerodha Broking | 67,91,541 | 14.73% | −5,313 |
| 3 | Angel One | 67,55,711 | 14.65% | +36,956 |
| 4 | ICICI Securities (ICICIdirect) | 21,60,619 | 4.68% | +4,410 |
| 5 | Upstox | 18,54,285 | 4.02% | −10,343 |
| 6 | Kotak Securities (Kotak Neo) | 13,91,840 | 3.02% | n/a |
| 7 | HDFC Securities (HDFC Sky / InvestRight) | 13,53,843 | 2.94% | +2,532 |
| 8 | Dhan (Raise Securities) | 11,20,901 | 2.43% | +13,624 |
| 9 | SBICAP Securities (SBI Securities) | 10,21,295 | 2.21% | −22,525 |
| 10 | Motilal Oswal Financial Services | 9,08,987 | 1.97% | n/a |

Just outside the top 10: Paytm Money 7,62,793 (#11), INDstocks/INDmoney 7,18,725 (#12), Sharekhan (#13), Axis (#14), IIFL (#15), 5paisa 3,21,174 (#16).

Caveat: I did not open the NSE workbook myself. The figures come from OneTradeJournal's copy and agree with Inc42 for the brokers Inc42 names.

---

## 2. Summary comparison table

Legend: BO = bracket order, CO = cover order, GTT = Good Till Triggered (or equivalent), OCO = one-cancels-other.

| Broker | Intraday brokerage | Delivery brokerage | DP charge per scrip on sell | NSE MIS square-off (post-CAS) | BO / CO | GTT / OCO | Official API, cost | Official MCP |
|---|---|---|---|---|---|---|---|---|
| Groww | min(₹20, 0.1%), min ₹5 [V] | Same as intraday [V] | ₹3.5 + ₹16.5 + 18% GST, about ₹23.60 [V] | CAS 3:05, others 3:20 [S] | BO: F&O only (Jan 2026) [S]; CO: [U] | GTT + OCO [V] | Groww Trade API, ₹499/mo + tax [V] | **Yes**: mcp.groww.in/mcp, OAuth, order tools described [V] |
| Zerodha | min(₹20, 0.03%) [V] | ₹0 [V] | ₹15.34 incl. GST [V] | CAS 3:12, others 3:25 [V] | BO: none since 2020; CO: yes [S] | GTT single + OCO [V] | Kite Connect: Personal free (no data); Connect ₹500/mo [S] | **Yes**: mcp.kite.trade/mcp, hosted read-only except GTT [V] |
| Angel One | min(₹20, 0.1%), min ₹5; ₹0 up to ₹500 in first 30 days [V] | Same as intraday [V] | ₹20 + GST, about ₹23.60 [V] | 3:15 (pre-CAS page) [V] | ROBO (BO) and CO in app [S] | GTT [S] | SmartAPI, free [S] | **No**: community only |
| ICICIdirect | ₹20 or 0.05%, lower (iValue, ₹299 one-time) [V] | Per base plan [U] | [U] | [U] | BO in iValue [V]; CO [U] | GTT in Breeze API [V] | Breeze API, free [V] | **No**: community only |
| Upstox | min(₹20, 0.1%) [V] | ₹20/order [V] | ₹20 + GST, about ₹23.60 [V] | CAS 3:10, others 3:25 [S] | CO discontinued 15 Jul 2026 [V]; BO [U] | GTT single/multi with trailing SL [V] | Upstox API, data free; order brokerage per schedule [S] | **Yes**: mcp.upstox.com/mcp, OAuth, **read-only** [V] |
| Kotak Neo | ₹10 or 0.05%, lower [V] | Trade Free 0.20%; Youth ₹0; Pro 0.10% [V] | [S] ~0.04% min ₹20–27 + GST | CAS and others: 3:05 PM (equity cash) [V, kotakneo.com, 2026-10-29] | BO and CO [V via API enum] | GTT [S] | Neo Trade API, ₹0 [V] | **Yes**: neo.kotaksecurities.com/mcp-server/mcp, QR login, **no orders** [V] |
| HDFC Securities | HDFC Sky: min(₹20, 2.5%) [V] | Same [V] | [U] | [U] | [U] | [U] | HDFC Sky Open API / InvestRight Individual API, free (company claim) [S] | **Yes (HDFC Sky)**: api.hdfcsky.com/mcp, 2FA login; order ability [U] [V] |
| Dhan | min(₹20, 0.03%) [V] | ₹0 [V] | ₹12.50 + GST, about ₹14.75 [V] | Equity 3:10 [V] | BO/CO as API product types [V] | Forever Order (SINGLE / OCO) [V] | DhanHQ: trading APIs free; data APIs paid [S] | **No**: community only |
| SBI Securities | ₹20/order (old) [S] | 0.35% (old) [S] | [U] | [U] | [U] | [U] | No public retail API found [U] | **No** |
| Motilal Oswal | Conflicting: ₹0, ₹20 or 0.02% [S] | ~0.20% [S] | ~₹18.5 [S] | 3:00 PM (Aug 2026 FAQ) [S] | [U] | GTC/GTD durations; GTT [U] | MO API ("moAPI"), cost [U] | **No** |

**Intraday leverage (all brokers).** SEBI's peak-margin rules (fully in force since Sep 2021) require brokers to collect at least VaR + ELM upfront for intraday equity, with a floor of about 20%. **Maximum effective intraday equity leverage is about 5x**, and it is lower for higher-VaR stocks. Brokers publish stock-wise MIS multipliers, for example Zerodha's bulletin: https://zerodha.com/marketintel/bulletin/249809/latest-intraday-leverages-mis-bo-co [S]. None of these brokers can legally exceed this for retail cash-equity intraday trades.

**Exchange transaction charges.** These are standard across brokers; only the published number differs.
- **NSE equity: 0.00307% from 1 Mar 2026** per Upstox [V]. Zerodha lists 0.00307% and Angel and Dhan list 0.0030699% [V]. Groww's page still shows 0.00297% [V], which is probably pre-March 2026 or excludes IPFT.
- **BSE groups A and B: 0.00375%** [V].
- For a tool, use **NSE 0.00307%** and **BSE 0.00375%** unless the broker states otherwise.

**SL-M (stop-loss market).**
- **BSE** discontinued SL-M in all segments from **9 Oct 2023** [V]: https://support.zerodha.com/category/trading-and-markets/kite-web-and-mobile/kite-error-messages/articles/slm-discontinued-by-bse
- **NSE** discontinued SL-M for options in 2021, per Upstox [S]. SL-M is still accepted for NSE cash equity at Zerodha, Upstox, Dhan, Groww and Kotak (API enums) [V].
- Upstox v3 and Kite require or accept `market_protection` on MARKET and SL-M orders. Upstox rejects `market_protection=0` for API market orders [V].

---

## 3. Per-broker detail

### 3.1 Groww (Rank 1)

**Charges** (https://groww.in/pricing)
- Equity brokerage, intraday and delivery: ₹20 or 0.1% per executed order, whichever is lower, with a ₹5 minimum [V].
- DP charge on sell: CDSL ₹3.5 (male) or ₹3.25 (female) plus Groww ₹16.5, then +18% GST. That is about **₹23.60** per scrip. Groww's part is ₹0 if the debit value is under ₹100 [V].
- NSE 0.00297%, BSE 0.00375% [V]. This may be stale; see the summary note.
- Auto square-off: **₹50 + GST per position** [V].
- MTF brokerage is 0.1% [V].

**Auto square-off time**
- Shares with F&O contracts (CAS stocks): **3:05 PM**. Other shares and F&O: **3:20 PM** [S]. Source: OneTradeJournal, checked 13 Oct 2026, citing https://groww.in/p/risk-policy .
- Older third-party pages say 3:10 or 3:15 PM, which is outdated.

**Order types**
- GTT and OCO ("Smart Orders") [V].
- Bracket orders launched for **F&O** on 29 Jan 2026: https://groww.in/updates/bracket-orders-on-groww [S].
- CO: [U].
- SL-M is supported in the API [V].

**API: Groww Trade API**
- Docs: https://groww.in/trade-api/docs/python-sdk
- Cost: **₹499/month + taxes** (early-bird; listed price ₹2,000): https://groww.in/trade-api [V]
- Rate limits for orders: 15/s, 250/min, 3,000/day [S, from the docs per search summary].
- Auth: API auth token [V]. Static-IP handling is not stated on the page; the SEBI rule applies anyway.

**Order placement**
- Call: `groww.place_order(...)` [V]: https://groww.in/trade-api/docs/python-sdk/orders
- Fields: `trading_symbol` (for example "WIPRO"), `quantity`, `price`, `trigger_price`, `validity` (DAY), `exchange` (NSE/BSE), `segment` (CASH/FNO), `product` (**CNC / MIS / NRML**), `order_type` (LIMIT / MARKET / SL / **SL_M**), `transaction_type` (BUY/SELL), `order_reference_id` (optional, 8–20 characters) [V]: https://groww.in/trade-api/docs/python-sdk/annexures
- GTT/OCO calls: `create_smart_order` (with `smart_order_type` GTT or OCO), `modify_smart_order`, `cancel_smart_order` [V]: https://groww.in/trade-api/docs/python-sdk/smart-orders
- GTT works with CNC in CASH. OCO in CASH is MIS only [V].

**MCP: OFFICIAL** [V]: https://groww.in/updates/groww-mcp (4 Aug 2025) and https://tech.groww.in/building-groww-mcp-a-technical-deep-dive-into-ai-powered-portfolio-intelligence-beea42be67f1
- Hosted endpoint: `https://mcp.groww.in/mcp`
- Login: OAuth via a browser login to Groww.
- Clients: Claude custom connector, Cursor, VS Code.
- Describes "Smart Order Management", meaning buy, limit and stop-loss orders can be placed. Selling needs DDPI.
- Cost of the MCP itself is not stated.

**Paper trading / sandbox:** none found [U].

### 3.2 Zerodha (Rank 2)

**Charges** (https://zerodha.com/charges) [V]
- Intraday: **min(₹20, 0.03%)** per executed order. Delivery: **₹0**.
- DP: **₹15.34 per scrip on sell, including GST** (₹3.5 CDSL + ₹9.5 Zerodha + ₹2.34 GST).
- NSE 0.00307%, BSE 0.00375%.
- **₹50 + GST per order** for call & trade *and* auto square-off.
- An extra ₹20 per order applies when trading on a debit balance.

**Auto square-off time** [V] (https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/intraday-auto-square-off-timings)
- CAS stocks: **3:12 PM**
- Non-CAS stocks: **3:25 PM**
- F&O: 3:26 PM
- Before CAS, equity moved from 3:20 PM to 3:25 PM on 26 Dec 2025 [S].

**Order types**
- BO disabled since March 2020 [S].
- **CO available**: intraday only, not BSE, not F&O [S].
- GTT single and OCO [V].
- SL-M available on NSE, blocked on BSE [V].
- Iceberg and auction varieties exist [V].

**API: Kite Connect v3**
- Docs: https://kite.trade/docs/connect/v3/
- Cost: the **Personal** tier is free (orders, GTT, portfolio; no market data). **Connect** is ₹500/month per app and adds WebSocket and historical data. Older pages say ₹2,000.
- Sources: https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/what-are-the-charges-for-kite-apis [S/V: official support page, mixed versions]

**Order placement** [V]: https://kite.trade/docs/connect/v3/orders/
- Call: `POST /orders/:variety`. Python call: `kite.place_order(variety=...)`.
- `variety`: regular / amo / co / iceberg / auction
- Fields: `exchange` (NSE/BSE/NFO/…), `tradingsymbol`, `transaction_type` (BUY/SELL), `quantity`, `product` (**MIS / CNC** / NRML / MTF), `order_type` (MARKET / LIMIT / SL / SL-M), `price`, `trigger_price`, `validity` (DAY / IOC / TTL), `market_protection` (−1 auto, or 1–100; MARKET and SL-M only), `tag`, `autoslice`.

**MCP: OFFICIAL**
- Repo: https://github.com/zerodha/kite-mcp-server (Go, MIT, zerodha GitHub org)
- Product page: https://zerodha.com/products/mcp/
- Support page: https://support.zerodha.com/category/trading-and-markets/general-kite/others-kite/articles/connect-zerodha-ai-assistant
- Hosted endpoint: `https://mcp.kite.trade/mcp` (legacy `/sse`), connected via `npx mcp-remote`.
- Login: a `login` tool returns a Kite login link (browser OAuth). No API key is needed for the hosted version.
- **The hosted version is read-only except GTT orders.** `place_order`, `modify_order` and `cancel_order` exist only when you self-host with your own Kite Connect key and secret [V].
- Hosted is free [S].

**Paper trading / sandbox:** no official sandbox [S]. Community forks such as Sundeepg98/kite-mcp-server add paper trading but are **community**.

### 3.3 Angel One (Rank 3)

**Charges** (https://www.angelone.in/exchange-transaction-charges) [V]
- Intraday and delivery: **min(₹20, 0.1%), minimum ₹5**, after an introductory ₹0 brokerage (up to ₹500) in the first 30 days.
- DP: **₹20 + GST per ISIN on sell** (₹3.5 CDSL + ₹16.5 Angel), about ₹23.60.
- NSE 0.0030699%. BSE A/B 0.00375%.
- Call & trade: ₹20/order.
- Auto square-off: "same as brokerage, no additional penalty" per the table, but the glossary is ambiguous.

**Auto square-off time**
- Equity **3:15 PM**, F&O 3:20 PM [V]: https://www.angelone.in/support/your-orders/square-off
- The page may predate CAS. Some Angel pages say 3:10 PM [S].

**Order types**
- ROBO (bracket-style) and CO in the app [S]: https://www.angelone.in/support/your-orders/robo-order
- GTT [S]: https://www.angelone.in/gtt-terms-and-condition
- SL-M as the API enum `STOPLOSS_MARKET` [S].

**API: SmartAPI**
- Docs: https://smartapi.angelone.in/docs (JavaScript-rendered; could not be fetched)
- SDK: https://github.com/angel-one/smartapi-python [V]
- Cost: free [S].
- Login: client code + PIN + **TOTP** via `generateSession` [V].

**Order placement**
- Call: `smartApi.placeOrder(orderparams)` (or `placeOrderFullResponse`). REST: `POST /rest/secure/angelbroking/order/v1/placeOrder` [S].
- Fields [V, README example]: `variety` (NORMAL; also STOPLOSS / AMO / ROBO [S]), `tradingsymbol` ("SBIN-EQ"), `symboltoken` ("3045"), `transactiontype` (BUY/SELL), `exchange` (NSE), `ordertype` (LIMIT / MARKET; STOPLOSS_LIMIT / STOPLOSS_MARKET [S]), `producttype` (**INTRADAY / DELIVERY**; also CARRYFORWARD / MARGIN / BO [S]), `duration` (DAY/IOC), `price`, `triggerprice`, `squareoff`, `stoploss`, `quantity`. **Values are strings.**
- GTT: `gttCreateRule`, `gttLists` [V].

**MCP: NONE OFFICIAL.** Community servers include pyalgobot/angelone-mcp, ameernoufil/angel-one-mcp (npm), bhavesh0009/angel-one-mcp-server, and MyFinanceMCP. All are community [S].

**Paper trading / sandbox:** none found [U].

### 3.4 ICICI Securities / ICICIdirect (Rank 4)

**Charges**
- The default plan structure was not captured; the official fee page returned "Technical Error 34".
- **iValue/Neo plan** (₹299 one-time): intraday **₹20 or 0.05%, lower**. Delivery brokerage stays on the existing base plan (Prime / iSecure) [V]: https://www.icicidirect.com/brokerage/neo-plan
- Demat AMC: ₹700, or ₹300 for iValue subscribers [V].
- DP per scrip: [U]. Square-off fee: [U].

**Auto square-off time:** [U].

**Order types**
- BO in the iValue plan ("intraday and bracket trades") [V]. CO: [U].
- GTT exists in Breeze (GTTOrderPlacement / GTTModifyOrder / GTTCancelOrder) [V].
- SL is via `order_type="stoploss"`. **Market orders are not permitted via Breeze** [V].

**API: Breeze API**
- Docs: https://api.icicidirect.com/breezeapi/documents/index.html
- Cost: **free** ("free of all charges", including historical data) [V]: https://api.icicidirect.com/apiuser/home
- Limits: 100 calls/min and 5,000/day. Orders are capped at **10/s** [V].
- **Static IP registered at API-key creation, changeable once a week** [V].
- **Regulatory restriction: placing, modifying or cancelling *Margin* (intraday) and *Option Plus* orders via Breeze is prohibited** [V]. In practice, MIS-style intraday via the API is blocked.

**Order placement** [V]
- Call: `POST https://api.icicidirect.com/breezeapi/api/v1/order`. Python: `breeze.place_order(...)`.
- Fields: `stock_code` (ICICI codes, for example "AXIBAN", not NSE symbols), `exchange_code` (NSE/NFO), `product` (cash / btst / futures / options), `action` (buy/sell), `order_type` (limit / stoploss), `stoploss` (trigger), `quantity`, `price`, `validity` (day/ioc), `validity_date`, `disclosed_quantity`, `user_remark`.

**MCP: NONE OFFICIAL.** "ICICI Direct MCP Server" on mcp.so is community [S].

**Paper trading / sandbox:** [U].

### 3.5 Upstox (Rank 5)

**Charges** (https://upstox.com/brokerage-charges/) [V]
- Intraday: **min(₹20, 0.1%)**. Delivery: **₹20/order**.
- DP: **₹20 + GST** (₹3.5 CDSL + ₹16.5), about ₹23.60.
- NSE 0.00307% from 1 Mar 2026.
- Call & trade: ₹75 + GST.
- Auto square-off: **₹75 + GST** (basic) or **₹50 + GST** (Upstox Plus); the page is internally inconsistent.

**Auto square-off time**
- CAS/F&O-stock shares **3:10 PM**, other shares 3:25 PM, F&O 3:25 PM [S]. Source: OneTradeJournal, citing https://upstox.com/help-center/252251/ .

**Order types**
- **CO discontinued for new orders from 15 Jul 2026** ("regulatory reasons", no circular named) [V]: https://upstox.com/announcements/updates/cover-order-co-functionality-is-being-discontinued-on-upstox/
- BO: [U]. Upstox suggests GTT as the replacement.
- **GTT**: SINGLE, or MULTIPLE (ENTRY / TARGET / STOPLOSS rules) with `trailing_gap` [V]. Endpoint: `POST https://api.upstox.com/v3/order/gtt/place`. Page: https://upstox.com/developer/api-documentation/place-gtt-order/
- SL-M supported [V].

**API: Upstox API (v2/v3)**
- Docs: https://upstox.com/developer/api-documentation/
- Cost: data and trading APIs free. Orders pay normal brokerage. A promotional ₹10/order API rate ended 31 Mar 2026, or 30 Sep 2026 per another source [S].
- Static-IP whitelist error UDAPI1154 [V].

**Order placement (v3)** [V]: https://upstox.com/developer/api-documentation/v3/place-order/
- Call: `POST https://api-hft.upstox.com/v3/order/place`. Optional header `X-Algo-Name` for exchange-approved algos.
- Fields: `instrument_token` ("NSE_EQ|INE…" style instrument key), `quantity`, `product` (**I** intraday / **D** delivery / MTF), `validity` (DAY/IOC), `price`, `order_type` (MARKET / LIMIT / SL / SL-M), `transaction_type` (BUY/SELL), `disclosed_quantity`, `trigger_price`, `is_amo` (ignored; auto-detected), `slice`, `market_protection` (−1 auto, 1–25, or 0 = none, which is rejected for API market orders), `tag` (≤40 characters).

**MCP: OFFICIAL, READ-ONLY** [V]: https://upstox.com/developer/api-documentation/mcp-integration/
- Hosted endpoint: `https://mcp.upstox.com/mcp`
- Login: OAuth in the browser, **re-authorized daily**.
- Free.
- Read-only: holdings, orders, positions, funds, MF, profile, quotes. **Cannot place orders.**
- A separate official **"upstox-skill"** Claude Code/Codex agent skill (uses upstox-python-sdk) *can* place orders: https://upstox.com/developer/api-documentation/announcements/agent-skills-launch/

**Sandbox: YES** [V]: https://upstox.com/developer/api-documentation/sandbox/
- Covers place, modify and cancel orders (v2 and v3) and multi-order.
- Separate sandbox app and token, valid 30 days, one app per user.
- Not a full paper-trading P&L simulator.

### 3.6 Kotak Securities / Kotak Neo (Rank 6)

**Charges** (https://www.kotakneo.com/pricing/) [V]
- Intraday: **₹10 or 0.05%, lower**, on all plans. ₹0 for the first 30 days.
- Delivery: Trade Free **0.20%**, Trade Free Youth (under 30) **₹0**, Trade Free Pro (₹249/mo) **0.10%**.
- **₹0 Trade API charges/brokerage.**
- DP: ~0.04%, minimum ~₹20–27 + GST [S, conflicting].
- Auto square-off: **₹50 + GST/order** [S, Kotak support page per search].

**Auto square-off time**
- **3:05 PM** for all equity/cash-segment MIS positions (CAS stocks and others alike) [V]: kotakneo.com support pages, read 2026-10-29.
  Source: https://kotaksecurities.com/support/what-are-the-auto-square-off-timings-for-open-intraday-positions
- F&O segment: 3:25 PM.
- Kotak does not publish a separate CAS-stock vs other-stock split for cash equity: 3:05 PM applies to both.

**Order types**
- BO and CO (API product enums; bracket help page) [V/S]: https://www.kotakneo.com/support/how-can-i-place-a-bracket-order-on-neo-platforms/
- GTT/GTC policy [S].
- SL-M in API enums [V].

**API: Kotak Neo Trade API**
- Landing page: https://www.kotakneo.com/landing-page/paid/kotak-neo-trade-api/
- Cost: free [V].
- Limit: 10 requests/s across APIs [S].
- The v2 SDK repo is **archived/legacy**: https://github.com/Kotak-Neo/Kotak-neo-api-v2 . The current package is `kotakneoapi` on PyPI (page did not render) [S].
- Login: consumer key, then `totp_login(mobile, ucc, totp)`, then `totp_validate(mpin)` [V, legacy README].

**Order placement** (legacy v2 SDK; may change in the new SDK) [V]
- Call: `client.place_order(...)`
- Fields: `exchange_segment` (nse_cm / bse_cm / nse_fo / …), `product` (**MIS / CNC** / NRML / CO / BO / MTF), `price`, `order_type` (L / MKT / SL / SL-M), `quantity`, `validity` (DAY / IOC / GTC / EOS / GTD), `trading_symbol`, `transaction_type` (**B / S**), `amo`, `disclosed_quantity`, `market_protection`, `trigger_price`, `tag`, plus BO fields.
- In REST JSON, the order type key is `pt` (for example "MKT") [S].

**MCP: OFFICIAL, NO ORDER PLACEMENT** [V]: https://www.kotakneo.com/platform/kotak-neo-mcp/
- Hosted endpoint: `https://neo.kotaksecurities.com/mcp-server/mcp` (via `npx mcp-remote`).
- Login: UCC, then a QR code scanned in the Neo mobile app.
- Holdings, order book, positions, research, live prices.
- Free.

**Paper trading / sandbox:** the SDK has an `environment` parameter ('prod'; UAT likely) [U].

### 3.7 HDFC Securities: HDFC Sky and InvestRight (Rank 7)

HDFC Securities runs two retail platforms: **HDFC Sky** (discount) and **InvestRight** (full-service). The NSE count is for the whole legal entity.

**Charges (HDFC Sky)** (https://hdfcsky.com/pricing) [V]
- Intraday and delivery: **₹20 or 2.5%, lower** (the 2.5% only bites below ₹800 of trade value).
- Auto square-off: **₹50 + 18% GST per position**, plus "RMS Sq up" brokerage.
- DP: [U] (separate demat page). Exchange charges: standard.

**Auto square-off time:** [U]. The support page returned 404.

**Order types**
- Market, limit, SL and SL-M in the app [S].
- BO/CO/GTT: [U].

**API**
- **HDFC Sky Open API**: developer portal https://developer.hdfcsky.com/ (JavaScript-rendered; field reference not retrieved) [S]. Claimed free [S]: https://hdfcsky.com/blogs/trading-strategies/hdfc-sky-open-api-guide
- App creation requires **Primary Static IP** (and optional secondary) [S]: https://docs.openalgo.in/connect-brokers/brokers/hdfc-sky
- **InvestRight Individual API**: https://developer.hdfcsec.com/ , free for InvestRight clients, up to 10 apps [S]: https://hdfcsec.com/blog/details/introducing-individual-api-for-investright-clients
- Order call name and field names: **[U]**.

**MCP: OFFICIAL (HDFC Sky)** [V, own blog, updated 10 Mar 2026]: https://hdfcsky.com/blogs/share-market/hdfc-sky-mcp-your-personal-ai-trading-companion
- Hosted endpoint: `https://api.hdfcsky.com/mcp`
- Login: HDFC Sky login + 2FA.
- Portfolio, research and alerts. **Order placement not stated [U].**
- The page's manual config sets `NODE_TLS_REJECT_UNAUTHORIZED=0`, which disables TLS verification and is a security smell.

**Paper trading / sandbox:** [U].

### 3.8 Dhan (Rank 8)

**Charges** (https://dhan.co/pricing/) [V]
- Intraday: **min(₹20, 0.03%)**. Delivery: **₹0**.
- DP: **₹12.50 + GST per ISIN** (about ₹14.75).
- NSE 0.0030699%. BSE 0.00375%.
- Auto square-off: **₹20 + GST/order**. Call & trade: ₹50 + GST.
- DhanHQ trading APIs listed as free.

**Auto square-off time**
- NSE equity **3:10 PM**, F&O 3:25 PM [V]: https://dhan.co/support/general/market-session-status-and-timing/what-are-the-dhan-intraday-auto-square-off-timings/
- No separate CAS/non-CAS split is shown.

**Order types**
- BO and CO as API `productType` values [V]. The "Super Order" API also exists [S].
- **Forever Order** = GTT (`orderFlag` SINGLE) or **OCO** [V]: https://dhanhq.co/docs/v2/forever/
- SL-M supported [V].

**API: DhanHQ v2**
- Docs: https://dhanhq.co/docs/v2/
- Trading APIs free. Data APIs are paid, around ₹499/mo, waivable with minimum monthly trades [S]: https://dhan.co/support/platforms/dhanhq-api/is-the-dhanhq-data-api-subscription-free-if-i-execute-a-minimum-number-of-trades-every-month/
- Order limits: 10/s, 250/min, 1,000/hr, 7,000/day. Maximum 25 modifications per order [V].
- **Static IP whitelisting required for place, modify and cancel** [V].

**Order placement** [V]: https://dhanhq.co/docs/v2/orders/
- Call: `POST https://api.dhan.co/v2/orders` with header `access-token`. Python: `dhan.place_order(...)`.
- Fields: `dhanClientId`, `correlationId`, `transactionType` (BUY/SELL), `exchangeSegment` (e.g. NSE_EQ), `productType` (**INTRADAY / CNC** / MARGIN / MTF / CO / BO), `orderType` (LIMIT / MARKET / STOP_LOSS / STOP_LOSS_MARKET), `validity` (DAY/IOC), `securityId` (numeric security ID), `quantity`, `price`, `triggerPrice`, `disclosedQuantity`, `afterMarketOrder`, `amoTime`, `boProfitValue`, `boStopLossValue`.

**MCP: NONE OFFICIAL found.** Community: vibeoski "Dhan MCP Server" (tagged "Official" on Glama but not by Dhan), harshitdynamite/DhanMCP, Vedhasagaran/dhan-mcp-py [S].

**Sandbox: YES, for integration testing** [V]: https://dhan.co/support/platforms/dhanhq-api/what-can-i-test-using-the-dhanhq-sandbox/
- Dhan explicitly says it is **not** paper trading and has no virtual-money simulation.

### 3.9 SBI Securities (Rank 9)

- **Charges:** official page content not retrievable. Third-party (Chittorgarh, last updated Jun 2024): intraday ₹20/order, delivery **0.35%**, AMC ₹500 [S]: https://www.chittorgarh.com/comparebroker/sbi-capital-vs-tradingo/19/169/
- DP: [U]. Square-off time and fee: [U].
- BO/CO/GTT: [U].
- **API:** no public retail trading API found [U].
- **MCP:** none found.
- **Paper trading:** none found.

### 3.10 Motilal Oswal (Rank 10)

**Charges** [S, conflicting]
- Delivery ~**0.20%**.
- Intraday: sources say ₹0, ₹20, or 0.02%.
- DP ~₹18.5 per scrip on sell. Source: https://chittorgarh.com/brokerage_charges/motilal-oswal/5 . Official page 404.

**Auto square-off time**
- **3:00 PM**, moved from 3:10 PM, per Motilal's Aug 2026 CAS FAQ [S]: https://www.motilaloswal.com/learning-centre/2026/8/closing-auction-session-complete-faq-guide

**Order types**
- LIMIT / MARKET / STOPLOSS.
- Durations DAY / GTC / GTD / IOC [V].
- GTT: [U]. BO/CO: [U]. A distinct SL-M type is not listed [V].

**API: MOFSL Trading APIs ("MO API")**
- Docs: https://invest.motilaloswal.com/moAPI/APIDocumentation/Introduction [V]
- Cost: [U] (third parties say free).
- **UAT test environment exists**: `https://openapi.motilaloswaluat.com` [V].
- A ClientPublicIp header and error MO2035 "Unauthorized IP Address" show IP checks [V].

**Order placement** [V]
- Call: `POST https://openapi.motilaloswal.com/rest/trans/v2/placeorder`
- Fields: `exchange` (NSE / BSE / NSEFO / …), `symboltoken`, `buyorsell` (BUY/SELL), `ordertype` (LIMIT / MARKET / STOPLOSS), `producttype` (**VALUEPLUS** = intraday-margin [S interpretation] / **DELIVERY** / NORMAL / SELLFROMDP / BTST / MTF), `orderduration` (DAY / GTC / GTD / IOC), `price`, `triggerprice`, `quantityinlot`, `amoorder` (Y/N), `goodtilldate`, **`algoid`**, `tag`.

**MCP:** none official. OpenAlgo's MCP can route to Motilal, but it is community [S].

---

## 4. Official MCP servers: summary

| Broker | Official? | Endpoint | Login | Can place orders? | Source |
|---|---|---|---|---|---|
| Zerodha | Yes | https://mcp.kite.trade/mcp (repo github.com/zerodha/kite-mcp-server) | Kite login link (OAuth) | Hosted: **GTT only**, otherwise read-only. Self-hosted with own key: full | [V] |
| Groww | Yes | https://mcp.groww.in/mcp | OAuth | **Yes** (described) | [V] |
| Upstox | Yes | https://mcp.upstox.com/mcp | OAuth, daily re-auth | **No** (read-only). The separate official agent skill can | [V] |
| Kotak Neo | Yes | https://neo.kotaksecurities.com/mcp-server/mcp | UCC + QR scan in app | **No** | [V] |
| HDFC Sky | Yes | https://api.hdfcsky.com/mcp | Sky login + 2FA | **Unknown** | [V] |
| Angel One, ICICIdirect, Dhan, SBI Securities, Motilal Oswal | **No official server found** | n/a | n/a | Community servers only | [S] |

---

## 5. SEBI retail algo-trading framework (relevant to personal API tools)

**Circulars**
- SEBI circular "Safer participation of retail investors in Algorithmic trading", **4 Feb 2025**. Reference SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 [S, number from memory and secondary sources].
- Extension to **1 Oct 2025**: SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/108, 29 Jul 2025 [S]: https://taxguru.in/sebi/sebi-extends-algo-trading-rule-implementation-1st-october-2025.html
- Glide path: SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/132, **30 Sep 2025** [S]. PDF on sebi.gov.in: https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf (fetched but text not extractable).
  - Brokers could go live from 1 Oct 2025.
  - Register at least one algo product and strategy by 31 Oct 2025.
  - API strategies registered by 30 Nov 2025.
  - Mock session by 3 Jan 2026.
  - Non-compliant brokers barred from onboarding new API-algo clients from 5 Jan 2026.
  - **Full implementation: 1 Apr 2026.** Source: https://www.outlookbusiness.com/amp/story/markets/sebi-extends-deadline-to-implement-retail-algo-trading-by-april-2026
- NSE implementation circular, 5–6 May 2025 (ref. INVG67858 per Zerodha) [S]: https://zerodha.com/z-connect/general/a-comprehensive-overview-of-nses-circular-on-the-new-retail-algo-trading-framework

**Rules that matter for a personal tool** (secondary unless noted)
1. **Static IP.** Order place, modify and cancel calls must come from a static IP registered against your API key/app. Data APIs are exempt.
   - Confirmed in official docs: Dhan [V], ICICI Breeze (changeable once a week) [V], Upstox (error UDAPI1154) [V], HDFC Sky (primary and secondary IP fields) [S].
   - Family members may share an IP. IPv6 is allowed. A non-India IP is not excluded (Zerodha reading).
2. **API key per client; no "open APIs".** Access must use OAuth/2FA. Most brokers force a **daily login / session expiry** (Upstox MCP re-auth daily; Kite access tokens are daily) [S].
3. **Order-rate threshold.** Up to **10 orders per second per exchange per segment, per API key**, without registration. A client may run only one unregistered key below the threshold. Above 10 OPS the strategy must be **registered with the exchange through the broker** and gets a unique **Algo ID**. Orders above the threshold are rejected; 5paisa returns HTTP 429.
4. **Algo ID tagging.**
   - Unregistered sub-threshold algos get a generic exchange ID, which the broker attaches.
   - Registered algos carry a strategy-specific ID. Examples: Upstox `X-Algo-Name` header [V], Motilal `algoid` field [V].
   - Vendor or broker-provided algos need registration regardless of rate.
   - Sources disagree on whether *every* API order carries an Algo ID (Sahi) or only registered algos do (5paisa).
5. **Broker liability.** Brokers are responsible for all API orders, so expect broker-side limits beyond SEBI's. Examples:
   - Kite: 3,000 orders/day internal cap [S].
   - Groww: 15/s, 250/min, 3,000/day [S].
   - Dhan: 10/s, 7,000/day [V].
   - ICICI Breeze: 10 orders/s and 5,000 calls/day [V]; **no market orders and no margin/intraday orders via API** [V].
6. **Market orders via API.** Brokers now require market protection (Upstox rejects `market_protection=0` for API market orders [V]; Kite supports a `market_protection` field [V]). Build tools to use limit orders, or send explicit market protection.

**Secondary sources for the SEBI section**
- https://aliceblueonline.com/blog/sebi-algo-trading-rules-retail-traders-april-2026
- https://fyers.in/community/t/sebis-new-algo-trading-rules-kick-in-on-april-1-here-is-what-changes-for-you/12267
- https://tradebetter.5paisa.com/t/important-update-sebi-algo-compliance-effective-1st-april/761
- https://www.tradejini.com/blogs/what-sebis-new-algo-trading-rules-mean-for-you
- https://www.sahi.com/blogs/sebi-algo-trading-rules-2026-what-every-retail-trader-must-know-before-april

---

## 6. Biggest uncertainties

1. **Square-off times after CAS (3 Aug 2026).** Many broker pages are stale. Only Zerodha (3:12/3:25), Dhan (3:10) and Kotak Neo (3:05 for equity cash) were verified on official pages. ICICI, HDFC and SBI are unknown or stale.
2. **HDFC Sky API field names and MCP order capability.** The developer portal is JavaScript-rendered and was not retrieved.
3. **SBI Securities.** Current charges and whether any retail API exists are both unknown.
4. **Motilal Oswal.** Intraday brokerage sources conflict, and API cost is unknown.
5. **Angel One SmartAPI enums** (STOPLOSS_MARKET, DELIVERY, ROBO). These come from the README and forums because the docs site would not render.
6. **Kotak Neo.** The new SDK's field names may differ from the archived v2 SDK.
7. **API pricing.** The Upstox per-order promo end date and Zerodha's ₹500 vs ₹2,000 data tier depend on page vintage.
8. **NSE transaction charge.** Groww shows 0.00297% while others show 0.00307% from 1 Mar 2026. Use 0.00307%.
9. **Exact SEBI circular text.** The PDF was not machine-readable here, so circular numbers come from secondary sources. Read the primary circulars on sebi.gov.in and nseindia.com circulars (not the API) before encoding rules.
