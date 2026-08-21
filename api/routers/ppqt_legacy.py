"""
api/routers/ppqt_legacy.py
──────────────────────────
LEGACY PPQT router (Wabtec-workbook model, mart-computed). Superseded by
api/routers/ppqt.py (LAMRES / EM-IE80-00003-B model). Kept for /ppqt-legacy.

Mounted at /api/ppqt-legacy in api/main.py.

Endpoints:
  GET /api/ppqt/health     — source mart status
  GET /api/ppqt/workcells  — workcells with both cycle-time and planner demand
  GET /api/ppqt/capacity   — full forward capacity computation for one workcell
  GET /api/ppqt/evidence   — per-assembly breakdown behind one process

PPQT has no mart of its own. It computes on demand by joining two existing
marts — cycle_time/raw.parquet and demand/planner_demand.parquet — see
modules/ppqt/capacity.py for the formulas and the data caveats.
"""

import logging

from fastapi import APIRouter, HTTPException, Query

from modules.ppqt.capacity import (
    CT_RAW,
    DEFAULTS,
    PLANNER_DEMAND,
    build_capacity,
    build_overview,
    default_month,
    line_matrix,
    list_months,
    list_workcells,
    process_evidence,
)

# PPQT is a monthly analysis (see modules/ppqt/capacity.py DEFAULTS). Every
# endpoint takes an optional YYYY-MM; omitted means the current calendar month.
_MONTH_Q = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
                 description="Demand month, YYYY-MM. Defaults to the current month.")

# PPQT sizes two resources off the same demand and takt (SMT!C40 — "Resources
# NEEDED (DLs per Shift or Equipment)"). `equipment` charges the full cycle time
# and compares to machines; `people` charges operator time only (no FPY) and
# compares to the IEDB headcount per station.
_RESOURCE_Q = Query("equipment", pattern="^(equipment|people)$",
                    description="equipment = machine sizing; people = DL/headcount sizing.")

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ppqt-legacy", tags=["PPQT (legacy)"])


@router.get("/health")
def ppqt_health():
    """PPQT reads two upstream marts — report both."""
    return {
        "status": "ok" if (CT_RAW.exists() and PLANNER_DEMAND.exists()) else "degraded",
        "sources": {
            "cycle_time_raw": {"path": CT_RAW.name, "exists": CT_RAW.exists()},
            "planner_demand": {"path": PLANNER_DEMAND.name, "exists": PLANNER_DEMAND.exists()},
        },
        "defaults": DEFAULTS,
    }


@router.get("/months")
def ppqt_months():
    """Months with planner demand — populates the period selector."""
    try:
        months = list_months()
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"count": len(months), "default": default_month(), "months": months}


@router.get("/workcells")
def ppqt_workcells(month: str | None = _MONTH_Q):
    """Workcells that have BOTH cycle-time data and planner demand, by demand desc."""
    try:
        df = list_workcells(month)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"month": month or default_month(), "count": len(df),
            "workcells": df.to_dict(orient="records")}


@router.get("/overview")
def ppqt_overview(
    shift_hours:  float = Query(DEFAULTS["shift_hours"],  ge=0, le=24),
    working_days: float = Query(DEFAULTS["working_days"], gt=0, le=400),
    co_qty:       float = Query(DEFAULTS["co_qty"],       ge=0, le=100),
    co_time:      float = Query(DEFAULTS["co_time"],      ge=0, le=600),
    efficiency:   float = Query(DEFAULTS["efficiency"],   gt=0, le=100),
    eq_avail:     int   = Query(DEFAULTS["eq_avail"],     ge=1, le=100),
    month:        str | None = _MONTH_Q,
    resource:     str = _RESOURCE_Q,
):
    """Capacity rollup for every workcell — the league table. ~3s cold, then cached."""
    m = month or default_month()
    try:
        rows = build_overview(shift_hours, working_days, co_qty, co_time,
                              efficiency, eq_avail, m, resource)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {
        "count": len(rows),
        "month": m,
        "resource": resource,
        "params": {
            "shift_hours": shift_hours, "working_days": working_days,
            "co_qty": co_qty, "co_time": co_time,
            "efficiency": efficiency, "eq_avail": eq_avail,
        },
        "workcells": list(rows),
    }


@router.get("/capacity")
def ppqt_capacity(
    workcell:     str = Query(..., description="Workcell name as it appears in /workcells (cycle-time spelling)."),
    shift_hours:  float | None = Query(None, ge=0,  le=24),
    working_days: float | None = Query(None, gt=0,  le=400),
    co_qty:       float | None = Query(None, ge=0,  le=100, description="Changeovers per day."),
    co_time:      float | None = Query(None, ge=0,  le=600, description="Minutes per changeover."),
    efficiency:   float | None = Query(None, gt=0,  le=100),
    eq_avail:     int   | None = Query(None, ge=1,  le=100, description="Machines available per process."),
    month:        str   | None = _MONTH_Q,
    resource:     str = _RESOURCE_Q,
):
    """
    Forward capacity computation for ONE month: planner demand × IEDB cycle time
    → WCT, Takt, Resources Needed, Gap, Utilisation — per process, per sub-workcenter.

    Shift parameters default to DEFAULTS in modules/ppqt/capacity.py and are
    overridable per request until the SBWC sheet is ingested.
    """
    try:
        return build_capacity(
            workcell, month=month, resource=resource,
            shift_hours=shift_hours, working_days=working_days,
            co_qty=co_qty, co_time=co_time,
            efficiency=efficiency, eq_avail=eq_avail,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/matrix")
def ppqt_matrix(
    workcell:       str = Query(...),
    sub_workcenter: str = Query(...),
    top:            int = Query(20, ge=1, le=200),
    month:          str | None = _MONTH_Q,
    resource:       str = _RESOURCE_Q,
):
    """Excel DASH 'TOP Demand': top-N assemblies × their CT at every process on the line."""
    try:
        return line_matrix(workcell, sub_workcenter, top, month, resource)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/evidence")
def ppqt_evidence(
    workcell:       str = Query(...),
    sub_workcenter: str = Query(...),
    process:        str = Query(...),
    month:          str | None = _MONTH_Q,
    resource:       str = _RESOURCE_Q,
):
    """Assemblies driving one process's WCT, ranked by demand × CT."""
    try:
        return process_evidence(workcell, sub_workcenter, process, month, resource)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
