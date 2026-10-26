Generator for the three scripts that carry the shared layers: nse-budget/size.py, nse-risk/check.py and
nse-broker/ticket.py. Edit the templates in tpl/ (the marker `#@@INDIA_CORE@@` is replaced by the whole of
_shared/india_core.py, verbatim: calendar, closing auction, brokers and charges, Yahoo, money, ticks) and tpl/fo.py (the
F&O stock list loader, `#@@FO@@`, used by budget and risk), then run `py _shared/build/build.py`. The broker profiles
and charges live in india_core.py only; ticket.py keeps one ticket renderer per broker (field names from
docs/brokers.md). Never edit the pasted blocks inside the generated scripts; the output is deterministic.
