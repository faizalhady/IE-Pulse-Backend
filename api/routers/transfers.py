"""
api/routers/transfers.py
────────────────────────
Cross-workcell man-hour transfer logs CRUD (SQLite).
Mounted under /api/transfers in api/main.py.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.deps import row_to_dict
from core.auth import verified_ntid
from core.database import get_conn
from modules.ole.config import WORKCELL_CONFIG

router = APIRouter(tags=["Transfers"])


class TransferCreate(BaseModel):
    date:    str
    shift:   int           # 1=Day  2=Night  3=Overtime
    from_wc: str
    to_wc:   str
    va_hc:   int  = 0
    va_hrs:  float = 0
    nva_hc:  int  = 0
    nva_hrs: float = 0


@router.get("/api/transfers")
def list_transfers(
    workcell:  Optional[str] = Query(None),   # matches from_wc OR to_wc
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    shift:     Optional[int] = Query(None),
):
    clauses, params = [], []
    if workcell:
        clauses.append("(from_wc = ? OR to_wc = ?)")
        params.extend([workcell, workcell])
    if date_from:  clauses.append("date >= ?");  params.append(date_from)
    if date_to:    clauses.append("date <= ?");  params.append(date_to)
    if shift:      clauses.append("shift = ?");  params.append(shift)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM transfer_logs {where} ORDER BY date DESC, shift",
            params
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.post("/api/transfers", status_code=201, dependencies=[Depends(verified_ntid)])
def create_transfer(body: TransferCreate):
    if body.from_wc not in WORKCELL_CONFIG:
        raise HTTPException(status_code=400, detail=f"Unknown workcell (from): {body.from_wc}")
    if body.to_wc not in WORKCELL_CONFIG:
        raise HTTPException(status_code=400, detail=f"Unknown workcell (to): {body.to_wc}")
    if body.from_wc == body.to_wc:
        raise HTTPException(status_code=400, detail="from_wc and to_wc cannot be the same")
    if body.shift not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="Shift must be 1 (Normal), 2 (Night), or 3 (Day)")
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO transfer_logs
               (date, shift, from_wc, to_wc, va_hc, va_hrs, nva_hc, nva_hrs)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (body.date, body.shift, body.from_wc, body.to_wc,
             body.va_hc, body.va_hrs, body.nva_hc, body.nva_hrs)
        )
        row = conn.execute("SELECT * FROM transfer_logs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@router.delete("/api/transfers/{log_id}", status_code=204,
               dependencies=[Depends(verified_ntid)])
def delete_transfer(log_id: int):
    with get_conn() as conn:
        deleted = conn.execute("DELETE FROM transfer_logs WHERE id = ?", (log_id,)).rowcount
    if not deleted:
        raise HTTPException(status_code=404, detail="Transfer not found")
