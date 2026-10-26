# ---- F&O stock list (closing-auction stocks); same file name in every skill, so one download serves all ----
FO_CACHE_DAYS = 7


def fo_cache_file(state_dir, d):
    return os.path.join(state_dir, "fo_mktlots-%s.csv" % d.isoformat())


def load_fo(state_dir, d, offline=False, path=None, write_cache=True):
    """(set of F&O stock symbols or None, note). `path` = a local fo_mktlots.csv. Otherwise today's cache
    nse/state/fo_mktlots-YYYY-MM-DD.csv, else a download (not offline), else the newest cache up to 7 days old
    (said in the note). With nothing at all it returns (None, note): callers then treat every stock as a
    closing-auction stock, which gives the earlier, safer flat_by."""
    if path:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse_fo_symbols(fh.read()), None
    today_file = fo_cache_file(state_dir, d)
    if os.path.exists(today_file):
        with open(today_file, encoding="utf-8", errors="replace") as fh:
            return parse_fo_symbols(fh.read()), None
    err = "offline"
    if not offline:
        try:
            text = _open_url(FO_LOTS_URL, accept="text/csv")
            if parse_fo_symbols(text):
                if write_cache:
                    os.makedirs(state_dir, exist_ok=True)
                    with open(today_file, "w", encoding="utf-8") as fh:
                        fh.write(text)
                return parse_fo_symbols(text), None
            err = "the download held no symbols"
        except (urllib.error.URLError, OSError) as e:
            err = str(e)
    for back in range(1, FO_CACHE_DAYS + 1):
        f = fo_cache_file(state_dir, d - datetime.timedelta(days=back))
        if os.path.exists(f):
            with open(f, encoding="utf-8", errors="replace") as fh:
                return parse_fo_symbols(fh.read()), (
                    "using the F&O stock list cached %d day%s ago (%s): it may be a little out of date"
                    % (back, "" if back == 1 else "s", err))
    return None, ("the F&O stock list (fo_mktlots.csv) is unavailable (%s): every stock is treated as a "
                  "closing-auction stock, so flat_by is earlier than it may need to be" % err)


def is_cas_stock(fo, sym):
    """A closing-auction stock = an F&O stock. An unknown list counts every stock as one (the cautious side)."""
    return True if fo is None else str(sym).upper() in fo
# ---- end F&O stock list ----
