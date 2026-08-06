"""The Python and SQL customer normalisers must agree.

They drifted once: the catalogue side keyed on `_cnorm` while the raw-mart
queries matched the customer string exactly. The marts carry some workcells
under two spellings (demand "MASIMO", IEDB "Masimo"), so the exact match found
nothing while the normalised lookup found the model — and a fully timed model
(Masimo 25959-AB, 25 steps, 25 cycle times) was reported as "in IEDB, nobody
has timed it".

Run: python tests/test_customer_key.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb

from modules.cycle_time.completion_v2 import _SQL_CNORM, _cnorm

CASES = [
    "Masimo", "MASIMO", "masimo", "  Masimo  ",
    "Life360", "LIFE 360", "life-360",
    "RESMED", "ResMed",
    "Keysight Technologies", "KEYSIGHT-TECHNOLOGIES",
    "", "  ",
]


def main() -> None:
    con = duckdb.connect()
    for s in CASES:
        got = con.execute(
            f"SELECT {_SQL_CNORM} FROM (SELECT ? AS customer)", [s]
        ).fetchone()[0]
        want = _cnorm(s)
        assert got == want, f"{s!r}: SQL gave {got!r}, Python gave {want!r}"

    # The whole point: different spellings collapse to one key.
    assert _cnorm("Masimo") == _cnorm("MASIMO") == _cnorm("  masimo ")
    # ...but genuinely different customers must NOT collapse.
    assert _cnorm("Masimo") != _cnorm("Masimo2")

    print(f"ok - {len(CASES)} cases, SQL and Python agree")


if __name__ == "__main__":
    main()
