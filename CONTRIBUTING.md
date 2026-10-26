# Contributing

Thanks for helping Indian traders trade with fewer surprises. Small, sourced
changes are the most valuable kind here.

## Good first contributions

- **Broker facts.** Confirm a broker's MIS auto square-off time after the
  closing auction, its DP charge, or an API field name. Link the broker's own
  page. Use the "Broker data" issue form, or edit `BROKERS` in
  `_shared/india_core.py` and move the field into `"verified"`.
- **Calendar.** Add next year's NSE holidays from NSE's trading-holidays
  circular (published every December) and Muhurat timings when NSE announces
  them.
- **Setups and tags.** A new scanner setup or a better news tag, with a test.

## Rules that keep this trustworthy

1. **Every market fact has a source.** Circular numbers or the broker's own
   page in a comment. A secondary source is fine if you say so.
2. **No scraping of protected sites.** No calls to `www.nseindia.com/api`,
   `api.bseindia.com`, cookie tricks or spoofed browser headers. Open files
   only (see the list in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).
3. **Tests run offline.** Save a small fixture under `tests/fixtures/` and test
   against it. CI runs the suite with the network blocked on Linux, Windows and
   macOS, Python 3.8 to 3.13.
4. **Shared layer first.** `_shared/india_core.py` is the source of truth for
   time, calendar, symbols, charges and brokers. Each skill carries a pasted
   copy, because skills install as standalone folders. Change the core, then
   re-paste: `python _shared/build/build.py` regenerates budget, risk and
   broker, and the other scripts are pasted by hand (their tests check the
   slice byte for byte).
5. **Never weaken the order gates.** Paper by default, risk PASS by
   fingerprint, the user's typed yes, and Claude's approval card. PRs that
   auto-place orders won't be merged. SEBI's retail-algo framework applies to
   automated order flow.
6. **No tips, no return claims.** In code, docs or examples.

## Running tests

```bash
python -m unittest discover -s tests -p "test_nse_*.py"
```

`test_nse_calendar_fresh.py` reads today's date on purpose and fails from
20 December when next year's holiday list is still missing.
