"""
scripts/test_freeze_history.py
──────────────────────────────
Self-check for compute._freeze_history — the rule that dates the share no
longer covers keep the numbers they were published with.

  python -m scripts.test_freeze_history

Runs against a throwaway parquet, never the real mart.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd                                     # noqa: E402

from modules.ole.pipeline import compute                # noqa: E402
from modules.ole.config import MART                     # noqa: E402


def frame(dates: list[str], ole: float) -> pd.DataFrame:
    return pd.DataFrame({
        "workcell": ["ASP"] * len(dates),
        "date": pd.to_datetime(dates),
        "shift": [1] * len(dates),
        "ole_pct": [ole] * len(dates),
    })


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="freeze_test_"))
    MART["ole"] = tmp / "ole_computed.parquet"           # redirect off the real mart

    # Days 1-9 published at 50%. Share now covers day 10 onward.
    published = frame(["2026-01-0%d" % d for d in range(1, 10)], 50.0)
    published.to_parquet(MART["ole"], index=False)

    # A recompute that would move EVERY day to 80% (e.g. an SMH backfill).
    recomputed = frame(["2026-01-%02d" % d for d in range(1, 15)], 80.0)

    out = compute._freeze_history(recomputed, pd.Timestamp("2026-01-10"))

    before = out[out["date"] < "2026-01-10"]
    after = out[out["date"] >= "2026-01-10"]

    assert len(out) == 14, out                           # 9 frozen + 5 fresh, no dupes
    assert set(before["ole_pct"]) == {50.0}, before      # history untouched
    assert set(after["ole_pct"]) == {80.0}, after        # share window recomputed
    assert out["date"].is_monotonic_increasing, out

    # No prior mart (first ever run) -> nothing to freeze, take the recompute whole.
    MART["ole"].unlink()
    assert len(compute._freeze_history(recomputed, pd.Timestamp("2026-01-10"))) == 14

    # Unreadable state file must NOT freeze everything — it means "recompute all".
    compute.STATE_FILE = tmp / "missing.json"
    assert compute._share_cutoff() is None

    print("freeze_history: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
