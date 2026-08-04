"""
api/routers/smh.py
──────────────────
CRUD over the `smh` table — Standard Man Hours per unit, the numerator of
every OLE number.

Endpoints
  GET    /api/smh                       list (filter by workcell / assembly search)
  GET    /api/smh/history               audit trail
  POST   /api/smh                       create one
  PUT    /api/smh/{workcell}/{assembly} update the value
  DELETE /api/smh/{workcell}/{assembly} remove the row
  POST   /api/smh/import                bulk import an .xls for one workcell

GET moved here from ole.py and now reads SQLite rather than smh_lookup.parquet.
The parquet is a snapshot from the last pipeline run; this page needs to show
what an edit just did, so it reads the live table.

Edits do NOT change any OLE number until the next pipeline refresh. That is
deliberate — see the `pending_refresh` flag on write responses, which the UI
uses to say so. Letting the UI trigger a refresh would let anyone kick off a
full recompute at will.

⚠️ Identity here is IDENTIFICATION, not AUTHENTICATION — same trade-off as
saved_reports.py. The caller sends its own NTID in `X-User-Ntid` and could send
anyone's. The allowlist stops accidents (a curious user clicking Edit), not a
determined one. Real auth needs the identity resolved server-side; until then
do not put anything here that would be damaging to forge.
"""

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from modules.ole import smh_store
from modules.ole.config import WORKCELL_CONFIG
from modules.ole.smh_store import SmhError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/smh", tags=["SMH"])

# Who may write. Two people today, so a list beats a lookup table.
# Grows past a handful -> move to workcell_pic (core/database.py already
# anticipates this) or an AD group, rather than editing code.
SMH_EDITORS = {"4033375", "1268287"}


class SmhCreate(BaseModel):
    workcell:  str = Field(..., min_length=1, max_length=80)
    assembly:  str = Field(..., min_length=1, max_length=120)
    smh_value: float = Field(..., gt=0)


class SmhUpdate(BaseModel):
    smh_value: float = Field(..., gt=0)


def _editor(ntid: Optional[str]) -> str:
    """Resolve + authorise the caller. Raises 401/403."""
    if not ntid or not ntid.strip():
        raise HTTPException(status_code=401, detail="X-User-Ntid header is required")
    who = ntid.strip()
    if who not in SMH_EDITORS:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to edit SMH values. Contact the IE team.",
        )
    return who


def _ok(payload: dict) -> dict:
    """Write responses carry the refresh caveat so the UI can surface it."""
    return {**payload, "pending_refresh": True}


# ─── Reads (open) ─────────────────────────────────────────────────────────────

@router.get("")
def list_smh(
    workcell: Optional[str] = Query(None),
    search:   Optional[str] = Query(None, description="Substring match on assembly"),
    limit:    int = Query(5000, ge=1, le=20000),
    offset:   int = Query(0, ge=0),
):
    return smh_store.list_smh(workcell=workcell, search=search, limit=limit, offset=offset)


@router.get("/health")
def health():
    return {"status": "ok", "rows": smh_store.count_smh()}


@router.get("/history")
def get_history(
    workcell: Optional[str] = Query(None),
    assembly: Optional[str] = Query(None),
    limit:    int = Query(200, ge=1, le=2000),
):
    return smh_store.history(workcell=workcell, assembly=assembly, limit=limit)


@router.get("/can-edit")
def can_edit(x_user_ntid: Optional[str] = Header(None)):
    """Lets the UI show or hide edit controls without guessing the rule."""
    return {"can_edit": bool(x_user_ntid and x_user_ntid.strip() in SMH_EDITORS)}


# ─── Writes (allowlisted) ─────────────────────────────────────────────────────

@router.post("")
def create_smh(body: SmhCreate, x_user_ntid: Optional[str] = Header(None)):
    who = _editor(x_user_ntid)
    cfg = WORKCELL_CONFIG.get(body.workcell, {})
    try:
        row = smh_store.create(
            body.workcell, body.assembly, body.smh_value, by=who,
            scan_stage=cfg.get("scan_stage"),
            stage_label=cfg.get("label"),
            plant=cfg.get("plant"),
        )
    except SmhError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _ok({"row": row})


@router.put("/{workcell}/{assembly:path}")
def update_smh(workcell: str, assembly: str, body: SmhUpdate,
               x_user_ntid: Optional[str] = Header(None)):
    who = _editor(x_user_ntid)
    try:
        row = smh_store.update(workcell, assembly, body.smh_value, by=who)
    except SmhError as e:
        raise HTTPException(status_code=404 if "No SMH row" in str(e) else 400, detail=str(e))
    return _ok({"row": row})


@router.delete("/{workcell}/{assembly:path}")
def delete_smh(workcell: str, assembly: str, x_user_ntid: Optional[str] = Header(None)):
    who = _editor(x_user_ntid)
    try:
        smh_store.delete(workcell, assembly, by=who)
    except SmhError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _ok({"deleted": {"workcell": workcell, "assembly": assembly}})


@router.post("/import")
async def import_xls(
    workcell: str = Form(...),
    file: UploadFile = File(...),
    x_user_ntid: Optional[str] = Header(None),
):
    """Bulk import one workcell's .xls — the same format IE already maintains.

    Which SMH column is read depends on the workcell's scan stage, so the
    workcell must be a known one; the parser cannot infer it from the file.
    """
    who = _editor(x_user_ntid)
    if workcell not in WORKCELL_CONFIG:
        raise HTTPException(status_code=400, detail=f"Unknown workcell: {workcell}")

    # Imported here, not at module load: ingest pulls in pandas/duckdb/bs4 and
    # this endpoint is the only thing in the router that needs them.
    from modules.ole.pipeline.ingest import _parse_smh_xls

    tmp_dir = Path(tempfile.mkdtemp(prefix="smh_import_"))
    tmp = tmp_dir / (file.filename or "upload.xls")
    try:
        with tmp.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        df = _parse_smh_xls(tmp, workcell)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if df.empty:
        raise HTTPException(
            status_code=400,
            detail="No SMH rows found in that file. Expected the IE SMH .xls export.",
        )

    usable = df[df["smh_value"] > 0]
    result = smh_store.upsert_many(usable.to_dict("records"), by=who, source="xls")
    result["parsed"] = len(df)
    result["skipped"] += len(df) - len(usable)     # blank cells parse to 0.0
    log.info("SMH import %s by %s: %s", workcell, who, result)
    return _ok(result)
