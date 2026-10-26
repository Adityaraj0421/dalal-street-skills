---
name: nse-news
description: >-
  News Monitor for NSE/BSE stocks. Tracks breaking news on every stock you
  watch or hold: Google News headlines (India edition) searched by company
  name and ticker, and how far each stock is from its previous close. Shows
  only what is new since the last check, newest first, in India time (IST),
  tags each item (results, dividend, bonus, split, buyback, order win, deal,
  block/bulk deal, rating/target, SEBI/regulatory, management change, fund
  raise, macro/RBI, circuit) and never repeats one. Use when the user says
  "any news on RELIANCE", "why is TCS moving", "what's the news on my
  watchlist", "morning briefing", "pre-market news", "monitor the news",
  "did anything break", "results date", "any dividend or bonus news", "block
  deal news", or after /nse-scanner finds setups.
argument-hint: "[tickers]"
---

# nse-news

On Windows, use `py` in place of `python3`.

A chart can look perfect and still be about to gap on a results announcement,
a SEBI notice or a block deal. Before a setup becomes a trade, this skill
reads the news for every stock on the watchlist and tells you only what is
new. It also prints how far each stock has moved today, so a headline and a
move sit side by side.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                         # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" RELIANCE TCS "M&M" --hours 12      # or --tickers ...
python3 "${CLAUDE_SKILL_DIR}/news.py" TCS --links --loose               # print links; show loose matches too
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-moves --json
```

Google News matches loosely, so a headline is shown only when its title names
one of your stocks (the ticker, any case for tickers of 5+ letters, or the
company name). The rest are counted as "loosely matched headlines hidden";
`--loose` shows them. Links are long Google redirects, so they are printed
only with `--links` (the JSON always has them). "management change" means a
change (appointed, resigns, steps down, new CEO), not a CEO being quoted.

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `nse/watchlist.json` | the scanner's hits (nse-scanner), with their company names |
| `nse/plan.json` | the trade you are planning (nse-exit) |
| `nse/portfolio.json` | what you hold (`positions` and `holdings`) |
| Google News RSS, India edition | one request per ticker: `https://news.google.com/rss/search?q="Reliance Industries" OR "RELIANCE share" when:48h&hl=en-IN&gl=IN&ceid=IN:en` |
| `nse/state/EQUITY_L-YYYY-MM-DD.csv` | NSE's list of equities (open file on archives.nseindia.com), cached for 7 days, to turn RELIANCE into "Reliance Industries" |
| Yahoo Finance chart endpoint | the last price against the previous close (unofficial, can lag) |
| `nse/state/news.json` | what has been shown (kept 30 days) |

Yahoo's headline feed is empty for Indian stocks, so it is not used.

## What it cannot see

NSE's corporate announcements (results dates, board meetings, dividend and
bonus record dates, insider and SEBI disclosures, shareholding patterns) are
filed on nseindia.com and BSE's site, but their data APIs are behind bot
protection, and this pack does not scrape them. Every run ends with a line
saying so. For anything that matters to a trade, tell the user to check
**nseindia.com > Corporate Announcements** (and the company's own investor
page) by hand for that ticker. A headline about results is a lead; the
exchange filing is the fact.

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story and read it. Quote the source and link it. Never
  summarise a paywalled story you could not read: say so. Google News also
  returns loosely related pages (market wraps, price-tracker pages): say when
  a headline is not really about the company.
- **Say what it touches.** Every item lists the tickers it touches and its
  tags: results, dividend, bonus, split, buyback, order win, deal, block/bulk
  deal, rating/target, SEBI/regulatory, management change, fund raise,
  macro/RBI, circuit. Tags come from the headline's words, so a tag is a hint.
- **MOVING** marks a stock 3 % or more from the previous close (`--move-pct`).
  A move with no headline is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is from
  the previous close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone, `--per-ticker 20`
   caps the headlines kept per stock.
2. Read the items that matter: anything tagged results, SEBI/regulatory,
   circuit, block/bulk deal or management change, and every MOVING ticker first.
3. Write a short brief, per ticker: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "results are
   due this week: an overnight gap can jump past any stop", or "a heavy
   delivery day plus a block deal headline: check who bought").
4. If a story changes the picture, say so before `/nse-entry` and `/nse-exit`:
   a results date inside the trade, a circuit, a fund raise. Never act on a
   headline alone. Remind the user to check Corporate Announcements by hand.

During the session: `/loop 15m /nse-news`, or a scheduled task each morning
before the 09:15 open.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per feed per ticker, a pause between
  calls.
- Never touches nseindia.com's website API or BSE's API.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/nse-broker`, after `/nse-risk`
passes and the user types an explicit yes for that order.
