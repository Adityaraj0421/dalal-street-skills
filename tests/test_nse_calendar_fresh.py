"""The one test that reads today's date on purpose: it fails when the NSE
holiday list is about to run out, so the weekly CI run asks for next year's
list (NSE publishes it in December) before the skills start guessing."""

import datetime
import importlib.util
import os
import unittest

from _load import ROOT

_spec = importlib.util.spec_from_file_location("india_core", os.path.join(ROOT, "_shared", "india_core.py"))
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)


class CalendarFresh(unittest.TestCase):
    def test_holiday_list_covers_today(self):
        today = datetime.date.today()
        last = max(core.CALENDAR_YEARS)
        deadline = datetime.date(last, 12, 20)
        self.assertLessEqual(
            today, deadline,
            "NSE holidays are only listed through %d. Add %d's list from NSE's trading-holidays circular to "
            "NSE_HOLIDAYS and CALENDAR_YEARS in _shared/india_core.py, re-paste that section into every "
            "nse-* script (build.py does budget, risk and broker), and update the tests." % (last, last + 1))


if __name__ == "__main__":
    unittest.main()
