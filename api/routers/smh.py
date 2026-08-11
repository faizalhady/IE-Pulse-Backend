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

Identity is AUTHENTICATED: every write needs a live AD_GET-signed token and
`admin` or above in `user_access` (core/auth.py). It used to trust a plain
`X-User-Ntid` header against a hardcoded pair of NTIDs, which anyone could send.
Reads stay open — SMH values are not sensitive, and the OLE pages read them.
"""

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from core.auth import RANK, level_of, optional_ntid, require_level
from modules.ole import smh_store
from modules.ole.config import WORKCELL_CONFIG
from modules.ole.smh_store import SmhError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/smh", tags=["SMH"])

# Who may write: admin or above in `user_access`, proven by AD_GET's signature.
# Was a hardcoded pair of NTIDs read from a self-asserted X-User-Ntid header —
# forgeable, and it went stale the moment the roster went live.
SMH_MIN_LEVEL = "admin"


class SmhCreate(BaseModel):
    workcell:  str = Field(..., min_length=1, max_length=80)
    assembly:  str = Field(..., min_length=1, max_length=120)
    smh_value: float = Field(..., gt=0)


class SmhUpdate(BaseModel):
    smh_value: float = Field(..., gt=0)


# Every write goes through this one dependency — the four handlers below cannot
# drift apart, and there is no second place to forget the check.
_editor = require_level(SMH_MIN_LEVEL)


def _ok(payload: dict) -> dict:
    """Write responses carry the refresh caveat so the UI can surface it."""
    return {**payload, "pending_refresh": True}


# ─── Reads (open) ─────────────────────────────────────────────────────────────

@router.get("")
def list_smh(
    workcell: Optional[str] = Query(None),
    search:   Optional[str] = Query(None, description="Substring match on assembly"),
    # The SMH page overlays the WHOLE live table on the coverage snapshot, so a
    # cap below the row count (32k and growing) silently hid edits: any row past
    # the cap kept showing the stale parquet value after a save.
    limit:    int = Query(100000, ge=1, le=200000),
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
def can_edit(ntid: Optional[str] = Depends(optional_ntid)):
    """Lets the UI show or hide edit controls without guessing the rule.

    Answers anonymous callers with false instead of 401 — a signed-out user
    needs a rendered page, not an error.
    """
    return {"can_edit": RANK[level_of(ntid)] >= RANK[SMH_MIN_LEVEL]}


# ─── Writes (allowlisted) ─────────────────────────────────────────────────────

@router.post("")
def create_smh(body: SmhCreate, who: str = Depends(_editor)):
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
               who: str = Depends(_editor)):
    try:
        row = smh_store.update(workcell, assembly, body.smh_value, by=who)
    except SmhError as e:
        raise HTTPException(status_code=404 if "No SMH row" in str(e) else 400, detail=str(e))
    return _ok({"row": row})


@router.delete("/{workcell}/{assembly:path}")
def delete_smh(workcell: str, assembly: str, who: str = Depends(_editor)):
    try:
        smh_store.delete(workcell, assembly, by=who)
    except SmhError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _ok({"deleted": {"workcell": workcell, "assembly": assembly}})


@router.post("/notify")
def notify(dry_run: bool = Query(True, description="Preview only. Set false to actually send."),
           recipients: Optional[str] = Query(None, description="Comma-separated. Default: everyone opted into ole_smh."),
           who: str = Depends(_editor)):
    """Email the SMH coverage digest — one table, every workcell, PIC per row.

    dry_run defaults to TRUE. This mails real colleagues, so sending has to be
    the deliberate choice rather than the one you get by forgetting a flag.
    """
    from modules.ole import smh_email

    rows = smh_email.build()
    to = ([t.strip() for t in recipients.split(",") if t.strip()]
          if recipients else smh_email.default_recipients())
    if dry_run:
        return {"dry_run": True, "to": to, "workcells": rows}

    try:
        sent = smh_email.send(rows, to)
    except RuntimeError as e:                 # unset PULSE_BASE_URL, no relay, no recipients
        raise HTTPException(status_code=503, detail=str(e))
    log.info("SMH digest sent to %d recipients, triggered by %s", sent, who)
    return {"dry_run": False, "sent": sent, "to": to, "workcells": rows}


@router.post("/import")
async def import_xls(
    workcell: str = Form(...),
    file: UploadFile = File(...),
    who: str = Depends(_editor),
):
    """Bulk import one workcell's .xls — the same format IE already maintains.

    Which SMH column is read depends on the workcell's scan stage, so the
    workcell must be a known one; the parser cannot infer it from the file.
    """
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
