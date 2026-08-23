"""
modules/universe/registry.py
────────────────────────────
The one place a workcell name becomes a workcell id.

Rule 5 of the universe: never join on a raw workcell name. The same workcell is
spelled five to seven ways across systems (case 2), so every name — planner,
cycle time, MES, payroll, a spreadsheet header — goes through here and comes out
as an id or None. None is an answer: it means "add an alias", never "guess".
"""

from __future__ import annotations

from functools import lru_cache

import duckdb

from core.naming import canon
from modules.universe.config import UNIVERSE_MART


@lru_cache(maxsize=1)
def _index() -> dict[str, int]:
    """canon(any known spelling) -> workcell_id, from dim_workcell + workcell_alias."""
    wc, al = UNIVERSE_MART["dim_workcell"], UNIVERSE_MART["workcell_alias"]
    if not (wc.exists() and al.exists()):
        return {}
    con = duckdb.connect()
    try:
        rows = con.execute(f"""
            select match_key, workcell_id from read_parquet('{wc.as_posix()}')
            union all
            select name, workcell_id from read_parquet('{wc.as_posix()}')
            union all
            select value, workcell_id from read_parquet('{al.as_posix()}')
        """).fetchall()
    finally:
        con.close()
    out: dict[str, int] = {}
    for value, wid in rows:
        key = canon(str(value))
        if key:
            out.setdefault(key, int(wid))
    return out


def resolve(name: str | None) -> int | None:
    """Workcell id for any spelling, or None when no alias knows it."""
    if not name:
        return None
    return _index().get(canon(str(name)))


def reset() -> None:
    """Forget the cached index (after a rebuild)."""
    _index.cache_clear()


if __name__ == "__main__":
    for n in ("KEYSIGHT", "K_CTEC", "ARISTANETWORKS", "BC (DANAHER)", "no such workcell"):
        print(f"{n!r:24} -> {resolve(n)}")
