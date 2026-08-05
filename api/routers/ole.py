"""
api/routers/ole.py
──────────────────
OLE module router — all endpoints that read OLE module parquets
(production, paid hours, SMH, ole_computed, ole_weekly, indirect labor,
mh distribution, shift operators) and the refresh trigger.

Endpoints kept at their historical paths for FE compatibility:
  GET  /api/health
  POST /api/refresh
  GET  /api/workcells
  GET  /api/shifts
  GET  /api/ole, /api/ole/summary, /api/ole/weekly, /api/ole/pareto
  GET  /api/ole/mh-breakdown, /api/ole/predict
  GET  /api/production
  GET  /api/paid-hours
  GET  /api/smh-status          (/api/smh + CRUD live in api/routers/smh.py)
  GET  /api/indirect-labor, /api/indirect-labor/entities
  GET  /api/shift-operators
  GET  /api/mh-distribution
"""

import json
import logging
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import con, parquet, df_to_json, where_clause, SHIFT_LABELS
from core.auth import require_level
from modules.ole.config import MART, WORKCELL_CONFIG, INDIRECT_LABOR_CONFIG

log = logging.getLogger(__name__)

router = APIRouter(tags=["OLE"])


@router.get("/api/health")
def health():
    """Mart presence AND ingest coverage.

    `mart_ready` only ever said "the files exist", which was true throughout
    the 2026-08-04 drift where the server was silently missing a day of
    production. `coverage` is written by every ingest run and is what actually
    answers "is this server up to date": status is `degraded` when the mart is
    missing a date the share still holds.
    """
    mart_files = {key: path.exists() for key, path in MART.items()}

    coverage = {}
    try:
        state_file = MART["production"].parent.parent / ".ingest_state.json"
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            coverage = state.get("coverage") or {}
            coverage["last_run"] = state.get("last_run")
    except Exception as e:                       # a broken state file must not 500 health
        coverage = {"error": str(e)}

    missing = coverage.get("missing_days") or []

    # Staleness matters as much as coverage. A failed ingest does NOT rewrite
    # the state file, so `coverage` would keep reporting the last GOOD run and
    # read as healthy while the mart quietly ages. Ingest runs daily; 36h gives
    # one missed run of slack before this complains.
    hours_old = None
    stale = False
    last_run = coverage.get("last_run")
    if last_run:
        try:
            hours_old = round(
                (pd.Timestamp.now() - pd.Timestamp(last_run)).total_seconds() / 3600, 1
            )
            stale = hours_old > 36
        except Exception:
            pass

    return {
        "status": "degraded" if (missing or stale or not all(mart_files.values())) else "ok",
        "mart_ready": all(mart_files.values()),
        "coverage_ok": not missing,
        "missing_days": missing,
        "stale": stale,
        "hours_since_last_ingest": hours_old,
        "coverage": coverage,
        "timestamp": pd.Timestamp.now().isoformat(),
        "mart_files": mart_files,
    }


@router.post("/api/refresh", dependencies=[Depends(require_level("admin"))])
def refresh():
    try:
        from modules.ole.pipeline.ingest import run as ingest_run
        from modules.ole.pipeline.compute import run as compute_run
        from modules.ole.pipeline.compute_weekly import run as weekly_run

        log.info("Refresh triggered via API")
        if not ingest_run():
            raise HTTPException(status_code=500, detail="Ingest failed — check server logs.")
        if not compute_run():
            raise HTTPException(status_code=500, detail="Compute failed — check server logs.")
        if not weekly_run():
            raise HTTPException(status_code=500, detail="Weekly compute failed — check server logs.")
        return {"status": "ok", "message": "Pipeline refresh complete."}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Refresh failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/workcells")
def get_workcells():
    return [
        {
            "workcell":    wc,
            "scan_stage":  cfg["scan_stage"],
            "stage_label": cfg["label"],
            "plant":       cfg["plant"],
            "smh_column":  cfg["smh_col"],
        }
        for wc, cfg in WORKCELL_CONFIG.items()
    ]


@router.get("/api/shifts")
def get_shifts():
    return [{"value": k, "label": v} for k, v in SHIFT_LABELS.items()]


@router.get("/api/ole")
def get_ole(
    workcell:  Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    shift:     Optional[int] = Query(None),
):
    c = con()
    try:
        parquet(c, "ole")
        clauses = []
        if workcell:   clauses.append(f"workcell = '{workcell}'")
        if date_from:  clauses.append(f"date >= '{date_from}'")
        if date_to:    clauses.append(f"date <= '{date_to}'")
        if shift:      clauses.append(f"shift = {shift}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        df = c.execute(f"SELECT * FROM ole {where} ORDER BY workcell, date, shift").df()
        return df_to_json(df)
    finally:
        c.close()


@router.get("/api/ole/summary")
def get_ole_summary(
    workcell:  Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    plant:     Optional[str] = Query(None),
):
    c = con()
    try:
        parquet(c, "ole")
        where = where_clause(workcell, date_from, date_to, date_col="date", plant=plant)
        sql = f"""
        SELECT
            workcell,
            stage_label,
            scan_stage,
            COUNT(*)                                          AS total_shifts,
            ROUND(AVG(ole_pct), 2)                           AS avg_ole_pct,
            ROUND(MIN(ole_pct), 2)                           AS min_ole_pct,
            ROUND(MAX(ole_pct), 2)                           AS max_ole_pct,
            COUNT(CASE WHEN data_quality = 'OK' THEN 1 END)  AS shifts_ok,
            COUNT(CASE WHEN data_quality != 'OK' THEN 1 END) AS flagged_shifts,
            ROUND(SUM(effective_output_smh), 2)              AS total_output_smh,
            ROUND(SUM(total_input_hours), 2)                 AS total_input_hours,
            SUM(total_qty)                                   AS total_qty,
            ROUND(AVG(hc_direct), 1)                         AS avg_hc_direct,
            MIN(date)                                        AS date_from,
            MAX(date)                                        AS latest_date
        FROM ole
        {where}
        GROUP BY workcell, stage_label, scan_stage
        ORDER BY workcell
        """
        return df_to_json(c.execute(sql).df())
    finally:
        c.close()


@router.get("/api/ole/weekly")
def get_ole_weekly(
    workcell:    Optional[str] = Query(None),
    sample_from: Optional[str] = Query(None),
    sample_to:   Optional[str] = Query(None),
    plant:       Optional[str] = Query(None),
):
    c = con()
    try:
        parquet(c, "ole_weekly")
        clauses = []
        if workcell:
            clauses.append(f"workcell = '{workcell}'")
        if plant:
            wcs = [wc for wc, cfg in WORKCELL_CONFIG.items() if cfg["plant"] == plant]
            if wcs:
                quoted = ", ".join(f"'{w}'" for w in wcs)
                clauses.append(f"workcell IN ({quoted})")
            else:
                clauses.append("1=0")
        if sample_from:
            clauses.append(f"week_start_date >= '{sample_from}'")
        if sample_to:
            clauses.append(f"week_start_date <= '{sample_to}'")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        df = c.execute(
            f"SELECT * FROM ole_weekly {where} ORDER BY workcell, iso_year, iso_week"
        ).df()
        return df_to_json(df)
    finally:
        c.close()


@router.get("/api/ole/pareto")
def get_ole_pareto(
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    plant:     Optional[str] = Query(None),
):
    c = con()
    try:
        parquet(c, "ole")
        where = where_clause(None, date_from, date_to, date_col="date", plant=plant)
        sql = f"""
        SELECT
            workcell,
            stage_label,
            ROUND(SUM(effective_output_smh), 2)  AS total_output_smh,
            ROUND(SUM(total_input_hours), 2)      AS total_input_hours,
            ROUND(SUM(va_hours), 2)               AS total_va_hours,
            SUM(total_qty)                        AS total_qty,
            ROUND(
                CASE WHEN SUM(total_input_hours) > 0
                THEN SUM(effective_output_smh) / SUM(total_input_hours) * 100
                ELSE NULL END, 2
            )                                     AS ole_pct,
            ROUND(
                CASE WHEN SUM(va_hours) > 0
                THEN SUM(effective_output_smh) / SUM(va_hours) * 100
                ELSE NULL END, 2
            )                                     AS ole_va_pct
        FROM ole
        {where}
        GROUP BY workcell, stage_label
        ORDER BY total_input_hours DESC
        """
        df = c.execute(sql).df()
        if df.empty:
            return []
        total_hrs = df["total_input_hours"].sum()
        df["cum_pct"] = (df["total_input_hours"].cumsum() / total_hrs * 100).round(1)
        df["va_pct"]  = df["ole_va_pct"]
        return df_to_json(df)
    finally:
        c.close()


@router.get("/api/ole/mh-breakdown")
def get_mh_breakdown(
    workcell:  Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    plant:     Optional[str] = Query(None),
):
    c = con()
    try:
        parquet(c, "paid_hours")
        parquet(c, "ole")

        ph_clauses = []
        if workcell:   ph_clauses.append(f"workcell = '{workcell}'")
        if date_from:  ph_clauses.append(f"date >= '{date_from}'")
        if date_to:    ph_clauses.append(f"date <= '{date_to}'")
        if plant:
            wcs = [wc for wc, cfg in WORKCELL_CONFIG.items() if cfg["plant"] == plant]
            if wcs:
                quoted = ", ".join(f"'{w}'" for w in wcs)
                ph_clauses.append(f"workcell IN ({quoted})")
            else:
                ph_clauses.append("1=0")
        ph_where = ("WHERE " + " AND ".join(ph_clauses)) if ph_clauses else ""

        ph_sql = f"""
        SELECT
            SUM(tph_direct)                                          AS total_input_hours,
            SUM(CASE WHEN value_type = 'VA'  THEN tph_direct ELSE 0 END) AS va_hours,
            SUM(CASE WHEN value_type = 'NVA' OR value_type = ''
                     THEN tph_direct ELSE 0 END)                     AS nva_hours,
            COUNT(CASE WHEN value_type = 'VA' THEN 1 END)            AS va_row_count
        FROM paid_hours
        {ph_where}
        """
        ph = c.execute(ph_sql).df().iloc[0]

        total_input_hours = float(ph["total_input_hours"] or 0)
        nva_hours         = float(ph["nva_hours"]         or 0)
        va_row_count      = float(ph["va_row_count"]      or 0)

        ole_where = where_clause(workcell, date_from, date_to, date_col="date", plant=plant)
        ole_sql = f"""
        SELECT ROUND(SUM(effective_output_smh), 4) AS output_smh
        FROM ole
        {ole_where}
        """
        output_smh = float(c.execute(ole_sql).df().iloc[0]["output_smh"] or 0)

        lunch_break = round(va_row_count * 0.25, 4)
        mfg_dt      = round(va_row_count * 1.3,  4)

        loss_pool       = total_input_hours - output_smh
        identified      = nva_hours + lunch_break + mfg_dt
        non_identified  = round(loss_pool - identified, 4)

        return {
            "total_input_hours": round(total_input_hours, 4),
            "loss_pool":         round(loss_pool, 4),
            "slices": [
                { "name": "Output SMH",             "value": round(output_smh, 4),  "color": "#22c55e" },
                { "name": "NVA Input",              "value": round(nva_hours, 4),   "color": "#ef4444" },
                { "name": "Lunch / Break",          "value": round(lunch_break, 4), "color": "#94a3b8" },
                { "name": "MFG DT",                 "value": round(mfg_dt, 4),      "color": "#f59e0b" },
                { "name": "Unexplained Lost Hours", "value": non_identified,        "color": "#6366f1" },
            ]
        }
    finally:
        c.close()


@router.get("/api/ole/predict")
def get_ole_predict(
    workcell:         str = Query(...),
    projection_weeks: int = Query(3, ge=1, le=12),
):
    try:
        from statsmodels.tsa.arima.model import ARIMA
        from statsmodels.tsa.holtwinters import ExponentialSmoothing
        import warnings
        warnings.filterwarnings("ignore")
    except ImportError:
        raise HTTPException(status_code=501, detail="statsmodels not installed.")

    c = con()
    try:
        parquet(c, "ole_weekly")
        df = c.execute(
            f"SELECT * FROM ole_weekly WHERE workcell = '{workcell}' ORDER BY iso_year, iso_week"
        ).df()
    finally:
        c.close()

    if df.empty:
        raise HTTPException(status_code=404, detail=f"No weekly data for workcell '{workcell}'")

    df = df.dropna(subset=["ole_pct"]).reset_index(drop=True)
    if len(df) < 4:
        raise HTTPException(status_code=422, detail=f"Not enough data ({len(df)} weeks, need >= 4)")

    series = df["ole_pct"].values.astype(float)
    n = len(series)
    seasonal_periods = 4 if n >= 8 else None

    arima_fitted = [None] * n
    hw_fitted    = [None] * n
    for i in range(3, n):
        try:
            fit = ARIMA(series[:i], order=(1, 1, 1)).fit()
            arima_fitted[i] = round(float(fit.forecast(steps=1)[0]), 2)
        except Exception:
            pass
        try:
            kwargs = dict(trend="add", initialization_method="estimated")
            if seasonal_periods and i >= seasonal_periods * 2:
                kwargs.update(seasonal="add", seasonal_periods=seasonal_periods)
            fit = ExponentialSmoothing(series[:i], **kwargs).fit(optimized=True)
            hw_fitted[i] = round(float(fit.forecast(1)[0]), 2)
        except Exception:
            pass

    rows = [
        {
            "workcell":   workcell,
            "week_label": row["week_label"],
            "iso_year":   int(row["iso_year"]),
            "iso_week":   int(row["iso_week"]),
            "actual":     round(float(row["ole_pct"]), 2),
            "arima":      arima_fitted[i],
            "hw":         hw_fitted[i],
            "projected":  False,
        }
        for i, row in df.iterrows()
    ]

    last_year, last_week = int(df["iso_year"].iloc[-1]), int(df["iso_week"].iloc[-1])

    try:
        arima_proj = [round(float(v), 2) for v in ARIMA(series, order=(1, 1, 1)).fit().forecast(steps=projection_weeks)]
    except Exception:
        arima_proj = [None] * projection_weeks

    try:
        kwargs = dict(trend="add", initialization_method="estimated")
        if seasonal_periods and n >= seasonal_periods * 2:
            kwargs.update(seasonal="add", seasonal_periods=seasonal_periods)
        hw_proj = [round(float(v), 2) for v in ExponentialSmoothing(series, **kwargs).fit(optimized=True).forecast(projection_weeks)]
    except Exception:
        hw_proj = [None] * projection_weeks

    py, pw = last_year, last_week
    for p in range(projection_weeks):
        pw += 1
        if pw > 52:
            pw = 1
            py += 1
        rows.append({
            "workcell":   workcell,
            "week_label": f"{py}-W{str(pw).zfill(2)}",
            "iso_year":   py,
            "iso_week":   pw,
            "actual":     None,
            "arima":      arima_proj[p] if p < len(arima_proj) else None,
            "hw":         hw_proj[p]    if p < len(hw_proj)    else None,
            "projected":  True,
        })

    return rows


@router.get("/api/production")
def get_production(
    workcell:  Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
):
    c = con()
    try:
        parquet(c, "production")
        where = where_clause(workcell, date_from, date_to, date_col="date")
        return df_to_json(c.execute(f"SELECT * FROM production {where} ORDER BY workcell, date, shift").df())
    finally:
        c.close()


@router.get("/api/paid-hours")
def get_paid_hours(
    workcell:  Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
):
    c = con()
    try:
        parquet(c, "paid_hours")
        where = where_clause(workcell, date_from, date_to, date_col="date")
        return df_to_json(c.execute(f"SELECT * FROM paid_hours {where} ORDER BY workcell, date, shift").df())
    finally:
        c.close()


# GET /api/smh now lives in api/routers/smh.py, backed by the `smh` SQLite
# table rather than smh_lookup.parquet. The parquet is a snapshot from the last
# pipeline run; the SMH page has to show what an edit just did.


@router.get("/api/smh-status")
def get_smh_status(
    workcell: Optional[str] = Query(None),
    status:   Optional[str] = Query(None),
):
    c = con()
    try:
        parquet(c, "smh_status")
        clauses = []
        if workcell:
            clauses.append(f"workcell = '{workcell}'")
        if status:
            clauses.append(f"smh_status = '{status}'")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return df_to_json(c.execute(
            f"SELECT * FROM smh_status {where} ORDER BY workcell, smh_status, total_qty_produced DESC"
        ).df())
    finally:
        c.close()


@router.get("/api/indirect-labor")
def get_indirect_labor(
    entity:    Optional[str] = Query(None),
    plant:     Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    shift:     Optional[int] = Query(None),
):
    c = con()
    try:
        parquet(c, "indirect_labor")
        clauses = []
        if entity:    clauses.append(f"entity = '{entity}'")
        if plant:     clauses.append(f"plant = '{plant}'")
        if date_from: clauses.append(f"date >= '{date_from}'")
        if date_to:   clauses.append(f"date <= '{date_to}'")
        if shift:     clauses.append(f"shift = {shift}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return df_to_json(c.execute(
            f"SELECT * FROM indirect_labor {where} ORDER BY entity, date, shift"
        ).df())
    finally:
        c.close()


@router.get("/api/indirect-labor/entities")
def get_indirect_labor_entities():
    return [
        {"entity": k, **v}
        for k, v in INDIRECT_LABOR_CONFIG.items()
    ]


@router.get("/api/shift-operators")
def get_shift_operators(
    workcell:  str = Query(...),
    date:      Optional[str] = Query(None),
    shift:     Optional[int] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
):
    if workcell not in WORKCELL_CONFIG and workcell not in INDIRECT_LABOR_CONFIG:
        raise HTTPException(status_code=400, detail=f"Unknown workcell: {workcell}")
    if shift is not None and shift not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="shift must be 1, 2, or 3")

    clauses = [f"workcell = '{workcell}'"]
    if date:      clauses.append(f"date = '{date}'")
    if shift:     clauses.append(f"shift = {shift}")
    if date_from: clauses.append(f"date >= '{date_from}'")
    if date_to:   clauses.append(f"date <= '{date_to}'")
    where = "WHERE " + " AND ".join(clauses)

    c = con()
    try:
        parquet(c, "paid_hours")
        rows = c.execute(
            f"""
            SELECT date, shift, position, value_type, total_input_hours,
                   thc_direct, tph_direct, sub_workcell, category
            FROM paid_hours
            {where}
            ORDER BY date, shift, value_type DESC, position
            """
        ).df()
        return df_to_json(rows)
    finally:
        c.close()


@router.get("/api/mh-distribution")
def get_mh_distribution(
    workcell:  Optional[str] = Query(None),
    plant:     Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    shift:     Optional[int] = Query(None),
):
    c = con()
    try:
        parquet(c, "mh_distribution")
        clauses = []
        if workcell:  clauses.append(f"workcell = '{workcell}'")
        if date_from: clauses.append(f"date >= '{date_from}'")
        if date_to:   clauses.append(f"date <= '{date_to}'")
        if shift:     clauses.append(f"shift = {shift}")
        if plant:
            wcs = [w for w, cfg in WORKCELL_CONFIG.items() if cfg.get("plant") == plant]
            if not wcs:
                return []
            in_list = ", ".join(f"'{w}'" for w in wcs)
            clauses.append(f"workcell IN ({in_list})")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return df_to_json(c.execute(
            f"SELECT * FROM mh_distribution {where} ORDER BY workcell, date, shift"
        ).df())
    finally:
        c.close()
