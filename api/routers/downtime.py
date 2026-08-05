"""
api/routers/downtime.py
───────────────────────
Downtime logs CRUD — supervisor-keyed production interruptions stored in SQLite.
Mounted under /api/downtime in api/main.py.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.deps import row_to_dict
from core.auth import verified_ntid
from core.database import get_conn
from modules.ole.config import WORKCELL_CONFIG

router = APIRouter(tags=["Downtime"])


class DowntimeCreate(BaseModel):
    date:        str
    shift:       int           # 1=Day  2=Night  3=Overtime
    workcell:    str
    bay:         str | None = None
    dept:        str
    code:        str
    dl_affected: int = 0
    minutes:     int
    commentary:  str | None = None


@router.get("/api/downtime")
def list_downtime(
    workcell:  Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    shift:     Optional[int] = Query(None),
):
    clauses, params = [], []
    if workcell:   clauses.append("workcell = ?");  params.append(workcell)
    if date_from:  clauses.append("date >= ?");     params.append(date_from)
    if date_to:    clauses.append("date <= ?");     params.append(date_to)
    if shift:      clauses.append("shift = ?");     params.append(shift)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM downtime_logs {where} ORDER BY date DESC, shift",
            params
        ).fetchall()
    return [row_to_dict(r) for r in rows]


# Writes need a signed-in caller, not a privileged one — logging downtime is
# the supervisor's daily job. Raise to require_level("admin") if that changes.
@router.post("/api/downtime", status_code=201, dependencies=[Depends(verified_ntid)])
def create_downtime(body: DowntimeCreate):
    if body.workcell not in WORKCELL_CONFIG:
        raise HTTPException(status_code=400, detail=f"Unknown workcell: {body.workcell}")
    if body.shift not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="Shift must be 1 (Normal), 2 (Night), or 3 (Day)")
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO downtime_logs
               (date, shift, workcell, bay, dept, code, dl_affected, minutes, commentary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (body.date, body.shift, body.workcell, body.bay,
             body.dept, body.code, body.dl_affected, body.minutes, body.commentary)
        )
        row = conn.execute("SELECT * FROM downtime_logs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@router.delete("/api/downtime/{log_id}", status_code=204,
               dependencies=[Depends(verified_ntid)])
def delete_downtime(log_id: int):
    with get_conn() as conn:
        deleted = conn.execute("DELETE FROM downtime_logs WHERE id = ?", (log_id,)).rowcount
    if not deleted:
        raise HTTPException(status_code=404, detail="Log not found")
