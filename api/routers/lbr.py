"""
api/routers/lbr.py
──────────────────
FastAPI router for the LBR module. Placeholder — overwrite with real
endpoints when the module is built out.

Mounted at /api/lbr in api/main.py.
  GET  /api/lbr/health
  POST /api/lbr/refresh
  GET  /api/lbr/data
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from core.auth import require_level

from modules.lbr.config import LBR_MART

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lbr", tags=["LBR"])


def _run_lbr_pipeline(mode: str) -> None:
    try:
        from modules.lbr.pipeline.refresh import run as run_refresh

        log.info(f"LBR pipeline started (mode={mode})")
        if not run_refresh(mode=mode):
            log.error("LBR pipeline failed — see logs above")
            return
        log.info(f"LBR pipeline complete (mode={mode})")
    except Exception:
        log.exception("LBR pipeline crashed")


@router.get("/health")
def lbr_health():
    return {
        "status": "placeholder",
        "mart": {key: {"exists": path.exists()} for key, path in LBR_MART.items()},
    }


@router.post("/refresh", status_code=202,
             dependencies=[Depends(require_level("admin"))])
def lbr_refresh(
    background_tasks: BackgroundTasks,
    mode: str = Query("incremental", pattern="^(incremental|full)$"),
):
    background_tasks.add_task(_run_lbr_pipeline, mode)
    return {"status": "accepted", "message": f"LBR pipeline started (mode={mode}). Placeholder — no real work yet."}


@router.get("/data")
def lbr_data():
    raise HTTPException(
        status_code=501,
        detail="LBR data endpoint not implemented yet — placeholder module.",
    )
