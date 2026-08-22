"""
api/routers/universe.py
───────────────────────
The Jabil Universe — health only in Phase 1. Serving comes with the semantic
views; nothing above this layer reads the parquet directly.

  GET /api/universe/health    which tables exist, row counts, build source
"""

from __future__ import annotations

import duckdb
from fastapi import APIRouter

from modules.universe.config import UNIVERSE_MART

router = APIRouter(prefix="/api/universe", tags=["universe"])


@router.get("/health")
def universe_health():
    tables = {}
    con = duckdb.connect()
    try:
        for name, path in UNIVERSE_MART.items():
            if path.exists():
                (n,) = con.execute(f"select count(*) from read_parquet('{path.as_posix()}')").fetchone()
                tables[name] = {"rows": n, "path": str(path)}
            else:
                tables[name] = {"rows": 0, "path": str(path), "missing": True}
    finally:
        con.close()
    return {"ok": all(not t.get("missing") for t in tables.values()), "tables": tables}
