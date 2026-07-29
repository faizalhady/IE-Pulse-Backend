"""
api/routers/saved_reports.py
────────────────────────────
Save / load named report content, keyed to the user's NTID.

Cross-cutting, so it follows the downtime/transfers pattern: router-only,
operational SQLite, no modules/ folder. One generic table serves every module —
see the `saved_reports` comment in core/database.py.

Endpoints
  GET    /api/saved-reports?module=&report_type=      list the caller's saves (no payload)
  GET    /api/saved-reports/{id}                      one save, with payload
  POST   /api/saved-reports                           create or overwrite by (owner, module, type, name)
  DELETE /api/saved-reports/{id}                      delete one of the caller's saves

Identity
────────
The caller sends its NTID (header `X-User-Ntid`, or the body on POST). The
frontend gets it from /userinfo/RetrieveUserInfoNoParam.

⚠️ This is IDENTIFICATION, not AUTHENTICATION. A caller can send any NTID, so
this partitions saves per user — it does not protect them. That is an accepted
trade-off for an internal tool storing report commentary. If anything sensitive
ever goes in `payload`, resolve the identity server-side instead and revisit
every handler here.
"""

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core.database import get_conn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/saved-reports", tags=["Saved Reports"])

MAX_PAYLOAD_BYTES = 1_000_000     # ~1 MB; a Q3 plan is a few KB


class SavedReportIn(BaseModel):
    module: str = Field(..., min_length=1, max_length=40)
    report_type: str = Field(..., min_length=1, max_length=40)
    name: str = Field(..., min_length=1, max_length=120)
    owner_ntid: str = Field(..., min_length=1, max_length=40)
    owner_name: Optional[str] = Field(None, max_length=200)
    owner_email: Optional[str] = Field(None, max_length=200)
    payload: Any


def _ntid(header_ntid: Optional[str]) -> str:
    if not header_ntid:
        raise HTTPException(status_code=400, detail="X-User-Ntid header is required")
    return header_ntid.strip()


@router.get("/health")
def health():
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM saved_reports").fetchone()["n"]
    return {"status": "ok", "saved_reports": n}


@router.get("")
def list_saved(
    module: str = Query(...),
    report_type: str = Query(...),
    x_user_ntid: Optional[str] = Header(None),
):
    """List the caller's saves. Payload is omitted — the list view doesn't need it."""
    ntid = _ntid(x_user_ntid)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, module, report_type, name, owner_ntid, owner_name,
                   created_at, updated_at
            FROM saved_reports
            WHERE module = ? AND report_type = ? AND owner_ntid = ?
            ORDER BY updated_at DESC
            """,
            (module, report_type, ntid),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/{report_id}")
def get_saved(report_id: int, x_user_ntid: Optional[str] = Header(None)):
    ntid = _ntid(x_user_ntid)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM saved_reports WHERE id = ? AND owner_ntid = ?",
            (report_id, ntid),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Saved report not found")
    out = dict(row)
    try:
        out["payload"] = json.loads(out["payload"])
    except (TypeError, ValueError):
        log.warning("saved_reports id=%s has unparseable payload", report_id)
        out["payload"] = None
    return out


@router.post("")
def save_report(body: SavedReportIn = Body(...)):
    """Create, or overwrite the caller's existing save with the same name.

    Overwrite-by-name is deliberate: "Save" on a name you already used should
    update it, not silently create a second row you can't tell apart.
    """
    payload_json = json.dumps(body.payload, ensure_ascii=False)
    if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO saved_reports
                (module, report_type, name, owner_ntid, owner_name, owner_email, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (module, report_type, owner_ntid, name) DO UPDATE SET
                payload     = excluded.payload,
                owner_name  = excluded.owner_name,
                owner_email = excluded.owner_email,
                updated_at  = datetime('now')
            """,
            (body.module, body.report_type, body.name.strip(), body.owner_ntid,
             body.owner_name, body.owner_email, payload_json),
        )
        row = conn.execute(
            """
            SELECT id, name, created_at, updated_at FROM saved_reports
            WHERE module = ? AND report_type = ? AND owner_ntid = ? AND name = ?
            """,
            (body.module, body.report_type, body.owner_ntid, body.name.strip()),
        ).fetchone()
    return dict(row)


@router.delete("/{report_id}")
def delete_saved(report_id: int, x_user_ntid: Optional[str] = Header(None)):
    ntid = _ntid(x_user_ntid)
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM saved_reports WHERE id = ? AND owner_ntid = ?",
            (report_id, ntid),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Saved report not found")
    return {"deleted": report_id}
