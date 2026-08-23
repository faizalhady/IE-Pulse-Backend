"""
tests/test_universe_refresh.py
──────────────────────────────
The daily job: `python -m modules.universe.pipeline.refresh` (no arguments) = run("incremental").
New MES days since the newest raw CSV → new payroll files → rebuild everything → a state
file the health endpoint reads. Written before the code. `python tests/test_universe_refresh.py`.
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _sandbox(raw_days: list[str]):
    """A temp raw dir with fake wipscan CSVs and a temp mart dir; fakes for the three steps."""
    from modules.universe import config as C
    from modules.universe.pipeline import refresh as R
    root = Path(tempfile.mkdtemp())
    raw = root / "wipscan"; raw.mkdir()
    for d in raw_days:
        (raw / f"wipscan_{d}.csv").write_text("x", encoding="utf-8")
    mart = root / "mart"; mart.mkdir()
    C.RAW_WIPSCAN_DIR = raw
    C.UNIVERSE_MART_DIR = mart
    calls: list[tuple] = []
    R.pull = lambda start, end, force=False, workers=4: calls.append(("pull", start, end)) or [raw / f"wipscan_{start}.csv"]
    R.pull_paid_hours = lambda: calls.append(("paid",)) or []
    import modules.universe.pipeline.build as B
    B.build_all = lambda: calls.append(("build",)) or {"fact_scan": 1}
    return R, calls, mart


def test_incremental_pulls_from_the_day_after_the_newest_file_to_yesterday_then_rebuilds():
    today = date.today()
    R, calls, mart = _sandbox([(today - timedelta(days=5)).isoformat(), (today - timedelta(days=4)).isoformat()])
    assert R.run("incremental") is True
    kinds = [c[0] for c in calls]
    assert kinds == ["pull", "paid", "build"], kinds
    _, start, end = calls[0]
    assert start == today - timedelta(days=3) and end == today, (start, end)       # [start, end): up to yesterday
    state = json.loads((mart / "refresh_state.json").read_text(encoding="utf-8"))
    assert state["ok"] is True and state["mode"] == "incremental" and state["days_pulled"] == 3
    assert state["finished"] and state["tables"] == {"fact_scan": 1}


def test_incremental_with_nothing_new_skips_the_pull_but_still_rebuilds():
    today = date.today()
    R, calls, mart = _sandbox([(today - timedelta(days=1)).isoformat()])
    assert R.run("incremental") is True
    assert [c[0] for c in calls] == ["paid", "build"], calls
    assert json.loads((mart / "refresh_state.json").read_text(encoding="utf-8"))["days_pulled"] == 0


def test_a_failure_is_recorded_not_raised():
    R, calls, mart = _sandbox([date.today().isoformat()])
    import modules.universe.pipeline.build as B
    B.build_all = lambda: (_ for _ in ()).throw(RuntimeError("duckdb exploded"))
    assert R.run("incremental") is False
    state = json.loads((mart / "refresh_state.json").read_text(encoding="utf-8"))
    assert state["ok"] is False and "duckdb exploded" in state["error"]


def test_full_mode_rebuilds_without_pulling():
    R, calls, mart = _sandbox([date.today().isoformat()])
    assert R.run("full") is True
    assert [c[0] for c in calls] == ["build"], calls


def test_paths_the_server_needs_are_overridable():
    """02 has no C:\\Users\\4033375: the registry (raw pulls), the skill references and the
    glossary are read from env-overridable paths so the same code runs there."""
    import importlib, os
    os.environ["UNIVERSE_REGISTRY_DIR"] = r"D:\x\registry"
    os.environ["UNIVERSE_SKILL_DIR"] = r"D:\x\skill"
    from modules.universe import config as C, tools as T
    importlib.reload(C); importlib.reload(T)
    try:
        assert str(C.REGISTRY_DIR) == r"D:\x\registry" and str(C.RAW_WIPSCAN_DIR).startswith(r"D:\x\registry")
        assert str(T.SKILL_DIR) == r"D:\x\skill"
    finally:
        os.environ.pop("UNIVERSE_REGISTRY_DIR"); os.environ.pop("UNIVERSE_SKILL_DIR")
        importlib.reload(C); importlib.reload(T)


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:                       # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {str(e)[:300]}")
            traceback.print_exc(limit=2)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
