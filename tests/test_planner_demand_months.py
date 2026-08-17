"""Month-header resolution for the planners' monthly demand grids.

This is the half of planner_demand.py that used to fail SILENTLY. The old manifest
hardcoded {col: 'YYYY-MM-DD'}, so the first time a planner rolled their sheet forward a
quarter, Oct/Nov/Dec would have been recorded as Jul/Aug/Sep with no error raised.
"""
from datetime import date

import pandas as pd

from modules.cycle_time.planner_demand import _month_cols


def cells(*vals):
    return pd.Series(list(vals))


def test_micron_typo_year_is_ignored():
    """Micron CTB.xlsx really does ship "Sept'27" for Sep-2026. The year in the cell is
    not trustworthy; the file's own date is."""
    got = _month_cols(cells("Area", "Model", " SAP Part Number", "Jul'26", "Aug'26", "Sept'27"),
                      date(2026, 7, 2))
    assert got == {3: date(2026, 7, 1), 4: date(2026, 8, 1), 5: date(2026, 9, 1)}


def test_cohu_bare_month_words_get_a_year():
    """COHU HLA - CTB.xlsx has no year anywhere in the header — just July/Aug/Sep."""
    got = _month_cols(cells(None, "Family", "Part number", "July", "Aug", "Sep"),
                      date(2026, 7, 4))
    assert got == {3: date(2026, 7, 1), 4: date(2026, 8, 1), 5: date(2026, 9, 1)}


def test_sequence_rolls_into_the_next_year():
    """Medtronic stamps every header 2026 even though the run goes Sep-26 -> Feb-27."""
    got = _month_cols(cells(*[f"2026-{m:02d}-01" for m in (9, 10, 11, 12, 1, 2)]),
                      date(2026, 7, 2))
    assert got[0] == date(2026, 9, 1)
    assert got[3] == date(2026, 12, 1)
    assert got[4] == date(2027, 1, 1)      # rolled, not back to Jan-26
    assert got[5] == date(2027, 2, 1)


def test_word_starting_with_a_month_is_not_a_month():
    assert _month_cols(cells("Marketing", "Mayor", "Junction", "Deck"), date(2026, 7, 1)) == {}


def test_bare_numbers_are_not_months():
    assert _month_cols(cells(10, 2026, -5, 12.0), date(2026, 7, 1)) == {}


def test_no_month_columns_returns_empty_so_the_caller_can_raise():
    assert _month_cols(cells("Part", "Description", "Total"), date(2026, 7, 1)) == {}


if __name__ == "__main__":     # ponytail: pytest is not installed on this box
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
