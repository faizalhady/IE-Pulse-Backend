"""
api/routers/ppqt.py
───────────────────
FastAPI router for the PPQT module — EM-IE80-00003-B template model
(LAMRES 8.0 workbook). Mounted at /api/ppqt in api/main.py.

  GET  /api/ppqt/health                         marts + workbooks status
  GET  /api/ppqt/workcells                      landing list: one row per workcell
  GET  /api/ppqt/4q?workcell=..&workcell=..     the 4Q report payload for a scope
  GET  /api/ppqt/{workcell}                     areas, periods, source files
  GET  /api/ppqt/{workcell}/summary             Exe Summaries: bays x periods, DL totals, NVA
  GET  /api/ppqt/{workcell}/stations            per-station metrics for one area + period
  GET  /api/ppqt/{workcell}/stations/{station}  top assemblies behind one station
  GET  /api/ppqt/{workcell}/assemblies          assemblies x stations CT grid for one area + period
  GET  /api/ppqt/{workcell}/inputs              every parameter the numbers come from
  POST /api/ppqt/refresh                        re-parse data/raw/ppqt/*.xlsx (background)

Formulas: modules/ppqt/compute.py. Ingest: modules/ppqt/pipeline/ingest.py.
Workcell names contain spaces ("LAM RESEARCH") - URL-encode them.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from modules.ppqt import compute
from modules.ppqt.config import PPQT_MART, PPQT_RAW_DIR

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ppqt", tags=["PPQT"])


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Not found: {e}")


@router.get("/health")
def ppqt_health():
    marts = {k: {"exists": p.exists(), "path": p.name} for k, p in PPQT_MART.items()}
    files = sorted(f.name for f in PPQT_RAW_DIR.glob("*.xlsx") if not f.name.startswith("~$"))
    return {"status": "ok" if all(m["exists"] for m in marts.values()) else "degraded",
            "marts": marts, "raw_dir": str(PPQT_RAW_DIR), "workbooks": files}


@router.get("/workcells")
def ppqt_workcells():
    rows = _guard(compute.list_workcells)
    return {"count": len(rows), "workcells": rows}


@router.get("/4q")
def ppqt_4q(workcell: list[str] = Query(..., description="Repeat for each workcell in scope"),
            drill_top: int = Query(3, ge=1, le=10)):
    """The 4Q report's numbers. Declared BEFORE /{workcell} or that route eats it."""
    return _guard(compute.fourq, tuple(workcell), drill_top)


@router.post("/refresh", status_code=202)
def ppqt_refresh(background: BackgroundTasks):
    from modules.ppqt.pipeline.refresh import run
    background.add_task(run, "full")
    return {"status": "started"}


@router.get("/{workcell}")
def ppqt_meta(workcell: str):
    return _guard(compute.workcell_meta, workcell)


@router.get("/{workcell}/summary")
def ppqt_summary(workcell: str):
    return _guard(compute.summary, workcell)


@router.get("/{workcell}/stations")
def ppqt_stations(workcell: str, area: str = Query(...), period: str = Query(...)):
    df = _guard(compute.station_metrics, workcell, area, period)
    if df.empty:
        raise HTTPException(status_code=404, detail=f"No PPQT sheet for {workcell} / {area} / {period}")
    rows = compute._records(df)
    return {"workcell": workcell, "area": area, "period": period, "count": len(rows),
            "totals": compute.area_totals(workcell, area, period),
            "line_groups": [{"group_no": int(g), "line_group": lg} for g, lg in
                            df.groupby("group_no")["line_group"].first().items()],
            "stations": rows}


@router.get("/{workcell}/stations/{station}")
def ppqt_station_assemblies(workcell: str, station: str, area: str = Query(...), period: str = Query(...),
                            top: int = Query(25, ge=1, le=500)):
    return _guard(compute.station_assemblies, workcell, area, period, station, top)


@router.get("/{workcell}/assemblies")
def ppqt_assemblies(workcell: str, area: str = Query(...), period: str = Query(...),
                    all: bool = Query(False, description="Include assemblies with zero demand")):
    return _guard(compute.assemblies, workcell, area, period, all)


@router.get("/{workcell}/inputs")
def ppqt_inputs(workcell: str):
    return _guard(compute.inputs, workcell)
