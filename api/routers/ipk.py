"""
api/routers/ipk.py
──────────────────
FastAPI router for the IPK module. Placeholder — overwrite with real
endpoints when the module is built out.

Mounted at /api/ipk in api/main.py.
  GET  /api/ipk/health
  POST /api/ipk/refresh
  GET  /api/ipk/data
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from core.auth import require_level

from modules.ipk.config import IPK_MART

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ipk", tags=["IPK"])


def _run_ipk_pipeline(mode: str) -> None:
    try:
        from modules.ipk.pipeline.refresh import run as run_refresh

        log.info(f"IPK pipeline started (mode={mode})")
        if not run_refresh(mode=mode):
            log.error("IPK pipeline failed - see logs above")
            return
        log.info(f"IPK pipeline complete (mode={mode})")
    except Exception:
        log.exception("IPK pipeline crashed")


@router.get("/health")
def ipk_health():
    return {
        "status": "placeholder",
        "mart": {key: {"exists": path.exists()} for key, path in IPK_MART.items()},
    }


@router.post("/refresh", status_code=202,
             dependencies=[Depends(require_level("admin"))])
def ipk_refresh(
    background_tasks: BackgroundTasks,
    mode: str = Query("incremental", pattern="^(incremental|full)$"),
):
    background_tasks.add_task(_run_ipk_pipeline, mode)
    return {"status": "accepted", "message": f"IPK pipeline started (mode={mode}). Placeholder — no real work yet."}


@router.get("/data")
def ipk_data():
    raise HTTPException(
        status_code=501,
        detail="IPK data endpoint not implemented yet — placeholder module.",
    )
