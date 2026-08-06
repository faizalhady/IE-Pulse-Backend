"""Self-checks for the 4Q completion history.

The whole point of Q4 is that complete + every loss sums back to 100%. If the
rollup drops a bucket or double-counts one, that sum is the first thing to move
-- and a 4Q report whose quadrants disagree is worse than no report.

Uses a synthetic frame, so it runs with no marts present.

Run: python tests/test_completion_history.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.cycle_time import completion_history as hist

ROWS = [
    {"customer": "Masimo", "assembly": "A1", "plant": "Plant 1", "units": 1000, "status": "complete", "reason": ""},
    {"customer": "Masimo", "assembly": "A2", "plant": "Plant 1", "units": 500, "status": "incomplete", "reason": "missing_ct"},
    {"customer": "Masimo", "assembly": "A3", "plant": "Plant 1", "units": 250, "status": "incomplete", "reason": "missing_ct"},
    {"customer": "Tellabs", "assembly": "B1", "plant": "JBK", "units": 200, "status": "not_in_iedb", "reason": "absent"},
    # No reason key at all, and no plant -- both happen in real rows.
    {"customer": "Tellabs", "assembly": "B2", "plant": None, "units": 50, "status": "not_in_mes"},
]


def main() -> None:
    now = datetime(2026, 8, 6)
    g = hist.rollup(ROWS, now)

    assert hist.iso_week(now) == "2026-W32", hist.iso_week(now)
    assert set(g["iso_week"]) == {"2026-W32"}

    # Every unit survives the rollup -- nothing dropped, nothing counted twice.
    assert int(g["units"].sum()) == 2000, g["units"].sum()
    assert int(g["models"].sum()) == len(ROWS)

    # The two missing_ct models collapse into ONE bucket of 750.
    mc = g[(g["status"] == "incomplete") & (g["reason"] == "missing_ct")]
    assert len(mc) == 1 and int(mc["units"].iloc[0]) == 750 and int(mc["models"].iloc[0]) == 2

    # A missing plant becomes Unassigned rather than vanishing from the split.
    assert "Unassigned" in set(g["plant"]), sorted(set(g["plant"]))
    # A missing reason becomes "", so it cannot split one bucket in two.
    assert g["reason"].isna().sum() == 0

    # Q4's invariant: complete + losses = 100%.
    total = int(g["units"].sum())
    done = int(g.loc[g["status"] == "complete", "units"].sum())
    loss = int(g.loc[g["status"] != "complete", "units"].sum())
    assert done + loss == total
    assert round(100 * done / total + 100 * loss / total, 6) == 100.0

    # An empty run must not explode -- it happens when a refresh finds nothing.
    assert hist.rollup([], now).empty

    print(f"ok - {len(g)} buckets, {total:,} units, complete+losses = 100%")


if __name__ == "__main__":
    main()
