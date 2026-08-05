"""
api/routers/ebuild.py
─────────────────────
FastAPI router for eBuild — the SMT build plan from the MES / eDashboard SQL
Server. Calls the stored procedure SP_GET_SY_SMT_BUILDPLAN directly via pyodbc.

This replaces the MES Hub's two-hop (Bun/Elysia route → sql_bridge.py). Since
IE-Pulse-Backend is already Python, one FastAPI endpoint does the whole job.

The data is MES-driven: each row's Sub_Status / UnitsCompleted / % /
CompletedDateTime reflect what was actually scanned/built on the floor —
`Quantity` is planned, `UnitsCompleted` is real production.

Runner ranking (Item 4): a full 24-month pull only takes ~2 min and the proc
already returns each job's *current* state, so we do a full refresh (no
incremental upsert needed) → aggregate units per (SMT_Assembly + Customer) →
store a small `runners.parquet`. The /runners endpoint reads + ranks that.

Endpoints:
  GET  /api/ebuild/buildplan?from=YYYY-MM-DD&to=YYYY-MM-DD   raw proc passthrough
  POST /api/ebuild/refresh?months=24                         rebuild runners mart (background)
  GET  /api/ebuild/refresh/status                            refresh progress
  GET  /api/ebuild/runners?customer=&order=top&limit=        ranked runners
"""

import logging
import os
from functools import lru_cache
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from core.auth import require_level
from core.mart_cache import mart_key

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ebuild", tags=["eBuild"])

# ─── Storage ─────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parents[2]        # IE-Pulse-Backend/
MART_DIR   = BASE_DIR / "data" / "mart" / "ebuild"

# Query timeout for the MES stored proc, seconds. See _fetch_buildplan.
MES_QUERY_TIMEOUT_S = int(os.getenv("MES_QUERY_TIMEOUT_S", "300"))

RUNNERS_PARQUET = MART_DIR / "runners.parquet"          # (customer, assembly) → units built
CUSTOMER_PLANT_PARQUET = MART_DIR / "customer_plant.parquet"  # customer → dominant plant (most units)
PLANT_RUNNERS_PARQUET = MART_DIR / "plant_runners.parquet"    # (plant, customer, assembly) → units + has_data
PROJECTION_RUNNERS_PARQUET = MART_DIR / "projection_runners.parquet"  # same shape, units = planned demand (SUM Quantity)
PLANNER_RUNNERS_PARQUET = MART_DIR / "planner_runners.parquet"        # same shape, units = planner-Excel demand (13wk); partial coverage

_normc = lambda s: str(s).strip().upper().replace("_", " ")  # customer name match (MES ↔ config)
_norma = lambda s: str(s).strip().upper()                     # assembly name match

# Refresh state (in-process, single source of truth for GET /refresh/status).
_REFRESH_STATE: dict = {"status": "idle", "started": None, "finished": None, "rows": None, "error": None}

# ─── MES / eDashboard SQL Server ──────────────────────────────────────────────
# Overridable via .env; defaults match the shared read-only account the MES Hub
# already uses (sql_bridge.py).
_MES_DRIVER   = os.getenv("MES_SQL_DRIVER",   "SQL Server")
_MES_SERVER   = os.getenv("MES_SQL_SERVER",   "AWASE1PENSQL01")
_MES_DATABASE = os.getenv("MES_SQL_DATABASE", "eDashboard_PEN")
_MES_UID      = os.getenv("MES_SQL_UID",      "eDashboard_MESystem")
_MES_PWD      = os.getenv("MES_SQL_PWD",      "mesystem")


def _conn_str() -> str:
    return (
        f"DRIVER={{{_MES_DRIVER}}};"
        f"SERVER={_MES_SERVER};"
        f"DATABASE={_MES_DATABASE};"
        f"UID={_MES_UID};"
        f"PWD={_MES_PWD};"
    )


def _fetch_buildplan(from_dt: datetime, to_dt: datetime) -> list[dict]:
    """Run SP_GET_SY_SMT_BUILDPLAN for [from_dt, to_dt] and return rows as dicts."""
    try:
        import pyodbc
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"pyodbc unavailable: {e}")
    try:
        conn = pyodbc.connect(_conn_str(), timeout=10)     # LOGIN timeout only
    except Exception as e:
        log.exception("MES SQL connect failed")
        raise HTTPException(status_code=503, detail=f"MES DB unavailable: {e}")
    # `timeout=` above bounds logging in, NOT running the query. Without this
    # line a mid-read network stall blocks forever: on 2026-08-03 exactly that
    # happened and the call sat for 51 MINUTES before dying with
    # "ConnectionRead (recv()) 10053". A healthy 24-month pull takes ~2-3 min
    # (measured: 365 days = 65s, 270k rows), so 5 min is generous headroom.
    conn.timeout = MES_QUERY_TIMEOUT_S
    try:
        cur = conn.cursor()
        cur.execute("EXEC [dbo].[SP_GET_SY_SMT_BUILDPLAN] @from=?, @to=?", from_dt, to_dt)
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _parse_day(s: str, end: bool) -> datetime:
    d = datetime.strptime(s.strip()[:10], "%Y-%m-%d")
    return d.replace(hour=23, minute=59, second=59) if end else d.replace(hour=0, minute=0, second=0)


# ─── Cycle-Time business rules (applied to both historical + projection marts) ──
_EOL_CUSTOMERS   = {"DYSON"}                          # EOL 2026-07 — excluded entirely
_PLANT1_OVERRIDE = {"MICRON SIG", "LAMGB", "LAMMEC"}  # physically BK, but Plant 1 supervision


def _apply_ct_rules(df: "pd.DataFrame") -> "pd.DataFrame":
    """Drop EOL workcells and honour plant-supervision overrides on raw buildplan."""
    cust_u = df["Customer"].astype(str).str.strip().str.upper()
    df = df[~cust_u.isin(_EOL_CUSTOMERS)].copy()
    df.loc[df["Customer"].astype(str).str.strip().str.upper().isin(_PLANT1_OVERRIDE), "Plant"] = "Plant 1"
    return df


# ─── Runner mart build (Item 4a/4b — full refresh) ────────────────────────────
def build_runners_mart(months: int = 24) -> int:
    """
    Pull the last `months` of buildplan, aggregate actual units built per
    (Customer + SMT_Assembly), and write runners.parquet. Returns row count.

    Runner metric = SUM(UnitsCompleted) (actual production, not planned Quantity).
    Grain = SMT_Assembly + Customer (the report is per-workcell = per-customer).
    """
    to_dt = datetime.now().replace(hour=23, minute=59, second=59, microsecond=0)
    from_dt = (to_dt - timedelta(days=round(months * 30.4))).replace(hour=0, minute=0, second=0)
    log.info("eBuild runner refresh: pulling buildplan %s → %s", from_dt.date(), to_dt.date())

    rows = _fetch_buildplan(from_dt, to_dt)
    MART_DIR.mkdir(parents=True, exist_ok=True)
    if not rows:
        pd.DataFrame(columns=["customer", "assembly", "units", "jobs", "last_completed"]).to_parquet(RUNNERS_PARQUET, index=False)
        pd.DataFrame(columns=["customer", "plant", "units"]).to_parquet(CUSTOMER_PLANT_PARQUET, index=False)
        pd.DataFrame(columns=["plant", "customer", "assembly", "units", "jobs", "last_completed", "has_data"]).to_parquet(PLANT_RUNNERS_PARQUET, index=False)
        return 0

    df = pd.DataFrame(rows)
    df["UnitsCompleted"] = pd.to_numeric(df.get("UnitsCompleted"), errors="coerce").fillna(0)

    df = _apply_ct_rules(df)   # drop EOL (DYSON) + plant-supervision overrides

    agg = (
        df.groupby(["Customer", "SMT_Assembly"], dropna=False)
        .agg(units=("UnitsCompleted", "sum"),
             jobs=("Order_ID", "nunique"),
             last_completed=("CompletedDateTime", "max"))
        .reset_index()
        .rename(columns={"Customer": "customer", "SMT_Assembly": "assembly"})
    )
    agg["units"] = agg["units"].astype(int)
    agg = agg[(agg["assembly"].notna()) & (agg["assembly"].astype(str).str.strip() != "")]
    agg.to_parquet(RUNNERS_PARQUET, index=False)

    # Dominant plant per customer = the plant with the most units built.
    plant = (
        df.groupby(["Customer", "Plant"], dropna=False)["UnitsCompleted"].sum()
        .reset_index().rename(columns={"Customer": "customer", "Plant": "plant", "UnitsCompleted": "units"})
    )
    plant["units"] = plant["units"].astype(int)
    dominant = plant.sort_values("units", ascending=False).drop_duplicates("customer").reset_index(drop=True)
    dominant.to_parquet(CUSTOMER_PLANT_PARQUET, index=False)

    # Plant-level runners: (plant, customer, assembly) → units built, + has_data
    # flag (ground truth = assembly_summary, same source the Cycle Time tab uses).
    pr = (
        df.groupby(["Plant", "Customer", "SMT_Assembly"], dropna=False)
        .agg(units=("UnitsCompleted", "sum"), jobs=("Order_ID", "nunique"), last_completed=("CompletedDateTime", "max"))
        .reset_index().rename(columns={"Plant": "plant", "Customer": "customer", "SMT_Assembly": "assembly"})
    )
    pr["units"] = pr["units"].astype(int)
    pr = pr[(pr["assembly"].notna()) & (pr["assembly"].astype(str).str.strip() != "")]
    lut = _has_data_lookup()
    pr["has_data"] = [(_norma(a) in lut.get(_normc(c), set())) for c, a in zip(pr["customer"], pr["assembly"])]
    pr.to_parquet(PLANT_RUNNERS_PARQUET, index=False)

    log.info("eBuild runner refresh: wrote %d (customer, assembly) rows, %d customer-plants, %d plant-runners",
             len(agg), len(dominant), len(pr))
    return len(agg)


def _has_data_lookup() -> dict:
    """norm(customer) → set(norm(assembly)) that HAVE cycle-time data, from
    assembly_summary.parquet. Ground truth for the has_data flag."""
    from modules.cycle_time.config import CT_MART
    p = CT_MART["assembly_summary"]
    if not p.exists():
        log.warning("assembly_summary mart missing — plant runners will all show no-data")
        return {}
    s = pd.read_parquet(p, columns=["customer", "assembly"])
    d: dict = {}
    for cust, asm in zip(s["customer"], s["assembly"]):
        d.setdefault(_normc(cust), set()).add(_norma(asm))
    return d


@lru_cache(maxsize=8)
def _runners_with_has_data(parquet_path: str, _key) -> pd.DataFrame:
    """Read a runners mart and recompute `has_data` from the CURRENT cycle-time
    mart, rather than trusting the value baked in when eBuild last ran.

    Why this exists
    ───────────────
    `has_data` was the ONLY reason the eBuild rebuild was chained onto the end
    of the cycle-time pipeline — refreshing one boolean meant re-pulling 24
    months of MES buildplan over SQL. On 2026-08-03 that turned a 10-minute
    cycle-time run into 67 minutes when MES dropped a connection.

    Computing it here instead makes the flag MORE accurate, not less: it now
    reflects the cycle-time mart as of this request, whereas the baked column
    reflected whenever eBuild last ran. And it lets the two pipelines run on
    their own schedules, failing independently.

    Cached on BOTH files' mtimes, so either one changing recomputes.
    Callers must treat the result as read-only (it is shared) — copy before
    mutating.
    """
    df = pd.read_parquet(parquet_path)
    if df.empty or "customer" not in df.columns or "assembly" not in df.columns:
        return df
    lut = _has_data_lookup()
    df = df.copy()
    df["has_data"] = [(_norma(a) in lut.get(_normc(c), set()))
                      for c, a in zip(df["customer"], df["assembly"])]
    return df


def _read_runners(parquet: Path) -> pd.DataFrame:
    """Runners mart with a freshly-computed has_data flag."""
    from modules.cycle_time.config import CT_MART
    return _runners_with_has_data(str(parquet), mart_key(parquet, CT_MART["assembly_summary"]))


PROJECTION_WEEKS = 4  # forward window MES buildplan actually populates (see docs)


def build_projection_runners_mart(weeks: int = PROJECTION_WEEKS) -> int:
    """Pull the next `weeks` of PLANNED buildplan → projection_runners.parquet:
    (plant, customer, assembly) → demand units (SUM Quantity) + jobs + has_data.

    Metric = SUM(Quantity) (planned demand), NOT UnitsCompleted — these jobs mostly
    aren't built yet. Trustworthy filter = Active job & Quantity > 0 (drops cancelled
    Active=False and downtime/placeholder Quantity=0 rows). We take ALL demand in the
    window with no kitting-status gate — a projection assumes planned jobs will run.
    Horizon capped at ~4 weeks: MES only populates the plan that far ahead.
    Same schema as plant_runners.parquet so the dashboard renders it unchanged.
    """
    from_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    to_dt   = (from_dt + timedelta(weeks=weeks)).replace(hour=23, minute=59, second=59)
    log.info("eBuild projection refresh: pulling planned buildplan %s → %s", from_dt.date(), to_dt.date())

    rows = _fetch_buildplan(from_dt, to_dt)
    MART_DIR.mkdir(parents=True, exist_ok=True)
    cols = ["plant", "customer", "assembly", "units", "jobs", "first_start", "planned_finish", "has_data"]
    if not rows:
        pd.DataFrame(columns=cols).to_parquet(PROJECTION_RUNNERS_PARQUET, index=False)
        return 0

    df = _apply_ct_rules(pd.DataFrame(rows))   # drop EOL + plant overrides
    df["Quantity"] = pd.to_numeric(df.get("Quantity"), errors="coerce").fillna(0)
    df["ProjectedStart"] = pd.to_datetime(df.get("ProjectedStart"), errors="coerce")
    df["ProjectedEnd"]   = pd.to_datetime(df.get("ProjectedEnd"), errors="coerce")
    # Trustworthy demand = active (not cancelled) jobs with a real quantity.
    active = df.get("Active")
    active_ok = (active.astype(str).str.strip().str.lower().isin(["true", "1"])
                 if active is not None else True)
    df = df[(df["Quantity"] > 0) & active_ok].copy()

    # A model spans several jobs → first_start = MIN start (when demand begins),
    # planned_finish = MAX end (when it's all due done).
    pr = (
        df.groupby(["Plant", "Customer", "SMT_Assembly"], dropna=False)
        .agg(units=("Quantity", "sum"), jobs=("Order_ID", "nunique"),
             first_start=("ProjectedStart", "min"), planned_finish=("ProjectedEnd", "max"))
        .reset_index().rename(columns={"Plant": "plant", "Customer": "customer", "SMT_Assembly": "assembly"})
    )
    pr["units"] = pr["units"].astype(int)
    pr = pr[(pr["assembly"].notna()) & (pr["assembly"].astype(str).str.strip() != "")]
    lut = _has_data_lookup()
    pr["has_data"] = [(_norma(a) in lut.get(_normc(c), set())) for c, a in zip(pr["customer"], pr["assembly"])]
    pr.to_parquet(PROJECTION_RUNNERS_PARQUET, index=False)
    log.info("eBuild projection refresh: wrote %d projection plant-runner rows", len(pr))
    return len(pr)


def _run_refresh(months: int):
    _REFRESH_STATE.update(status="running", started=datetime.now().isoformat(), finished=None, rows=None, error=None)
    try:
        n = build_runners_mart(months)
        try:
            build_projection_runners_mart()   # near-term demand list (non-fatal)
        except Exception:
            log.exception("eBuild projection refresh failed (non-fatal)")
        _REFRESH_STATE.update(status="success", finished=datetime.now().isoformat(), rows=n)
    except Exception as e:
        log.exception("eBuild runner refresh failed")
        _REFRESH_STATE.update(status="error", finished=datetime.now().isoformat(), error=str(e))


# ─── Endpoints ────────────────────────────────────────────────────────────────
@router.get("/buildplan")
def ebuild_buildplan(
    from_: str = Query(..., alias="from", description="Start date, YYYY-MM-DD (inclusive)."),
    to:    str = Query(..., alias="to",   description="End date, YYYY-MM-DD (inclusive)."),
):
    """
    SMT build plan for [from, to] straight from MES — raw proc passthrough.

      → { "count": N, "rows": [ { Order_ID, Plant, Customer, Bay, JobNumber,
            SMT_Assembly, Rev, Quantity, UnitsCompleted, Status, Sub_Status,
            CompletedDateTime, ... }, ... ] }
    """
    try:
        from_dt, to_dt = _parse_day(from_, end=False), _parse_day(to, end=True)
    except ValueError:
        raise HTTPException(status_code=400, detail="from/to must be YYYY-MM-DD")
    if from_dt > to_dt:
        raise HTTPException(status_code=400, detail="'from' must be on or before 'to'")
    try:
        rows = _fetch_buildplan(from_dt, to_dt)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("SP_GET_SY_SMT_BUILDPLAN failed")
        raise HTTPException(status_code=502, detail=f"MES query failed: {e}")
    return {"count": len(rows), "rows": rows}


@router.post("/refresh", dependencies=[Depends(require_level("admin"))])
def ebuild_refresh(background: BackgroundTasks, months: int = Query(24, ge=1, le=60)):
    """Rebuild the runners mart in the background. Poll GET /refresh/status."""
    if _REFRESH_STATE["status"] == "running":
        return {"status": "running", "detail": "A refresh is already in progress."}
    background.add_task(_run_refresh, months)
    return {"status": "started", "months": months}


@router.get("/refresh/status")
def ebuild_refresh_status():
    return _REFRESH_STATE


@router.get("/customer-plants")
def ebuild_customer_plants():
    """
    Dominant plant per customer (the plant where it built the most units), from
    the buildplan. Some customers build in >1 plant — this returns the biggest.

      → { "count": N, "plants": [ { customer, plant, units }, ... ] }
    """
    if not CUSTOMER_PLANT_PARQUET.exists():
        raise HTTPException(status_code=503, detail="Customer-plant mart not built. POST /api/ebuild/refresh first.")
    df = pd.read_parquet(CUSTOMER_PLANT_PARQUET)
    plants = [{"customer": r["customer"], "plant": r["plant"], "units": int(r["units"])}
              for r in df.to_dict(orient="records")]
    return {"count": len(plants), "plants": plants}


def _section(label: str, sub: "pd.DataFrame", top: int) -> dict:
    """Build a dashboard section (KPI totals + top-N runners) from a frame with
    columns customer, assembly, units, jobs, last_completed, has_data —
    one row per (customer, assembly)."""
    runner_count = int(len(sub))
    with_data = int(sub["has_data"].sum())
    top_df = sub.sort_values("units", ascending=False).head(top).reset_index(drop=True)

    def _iso(v):
        return None if v is None or pd.isna(v) else (v.isoformat() if hasattr(v, "isoformat") else str(v))

    runners = []
    for i, r in enumerate(top_df.to_dict(orient="records")):
        runners.append({
            "rank": i + 1,
            "customer": r["customer"],
            "assembly": r["assembly"],
            "plant": r.get("plant"),
            "units": int(r["units"]),
            "jobs": int(r["jobs"]) if pd.notna(r.get("jobs")) else 0,
            "last_completed": _iso(r.get("last_completed")),   # historical: last built
            "first_start": _iso(r.get("first_start")),         # projection: demand starts
            "planned_finish": _iso(r.get("planned_finish")),   # projection: all due done
            "has_data": bool(r["has_data"]),
        })
    return {
        "plant": label,
        "total_units": int(sub["units"].sum()),
        "runner_count": runner_count,
        "with_data": with_data,
        "no_data": runner_count - with_data,
        "runners": runners,
    }


# Region → the plants it covers. Penang Island = the island plants; Batu Kawan =
# the mainland plant. (Plant 3 / Plant 8 are negligible and intentionally omitted.)
_REGIONS = [
    ("Batu Kawan",    ["JBK"]),
    ("Penang Island", ["Plant 1", "JPE"]),
]


def _agg_with_plant(sub: "pd.DataFrame") -> "pd.DataFrame":
    """Collapse a plant_runners subset to one row per (customer, assembly): sum
    units/jobs, has_data if any, + dominant plant. Date columns depend on the
    mart: historical has `last_completed`; projection has `first_start` /
    `planned_finish` — aggregate whichever are present."""
    aggs = {"units": ("units", "sum"), "jobs": ("jobs", "sum"), "has_data": ("has_data", "max")}
    if "last_completed"  in sub.columns: aggs["last_completed"]  = ("last_completed", "max")
    if "first_start"     in sub.columns: aggs["first_start"]     = ("first_start", "min")
    if "planned_finish"  in sub.columns: aggs["planned_finish"]  = ("planned_finish", "max")
    agg = sub.groupby(["customer", "assembly"], as_index=False).agg(**aggs)
    dom = (sub.sort_values("units", ascending=False)
              .drop_duplicates(["customer", "assembly"])[["customer", "assembly", "plant"]])
    return agg.merge(dom, on=["customer", "assembly"], how="left")


@router.get("/plant-runners")
def ebuild_plant_runners(
    top:    int = Query(50, ge=1, le=500, description="Top-N runners per section."),
    plants: int = Query(3,  ge=1, le=20,  description="Number of plants (biggest by units)."),
    mode:   str = Query("historical", pattern="^(historical|projection|planner)$",
                        description="historical = units built (24mo); projection = MES planned demand (~4wk); "
                                    "planner = planners' Excel demand (~13wk, partial workcell coverage)."),
):
    """
    Plant dashboard: an "Overall Penang" section (all plants combined) plus the
    biggest `plants` by units, each with KPI totals + its top `top` runners
    (units built per assembly, badged has-data).

      → { "as_of": iso,
          "overall": { plant:"Overall Penang", total_units, runner_count,
                       with_data, no_data, runners:[...] },
          "plants": [ { plant, total_units, runner_count, with_data, no_data,
                        runners:[ { rank, customer, assembly, units, jobs,
                                    last_completed, has_data } ] } ] }
    """
    parquet = {"projection": PROJECTION_RUNNERS_PARQUET,
               "planner": PLANNER_RUNNERS_PARQUET}.get(mode, PLANT_RUNNERS_PARQUET)
    if not parquet.exists():
        raise HTTPException(status_code=503, detail=f"{mode} plant-runners mart not built. POST /api/ebuild/refresh first.")
    df = _read_runners(parquet)

    as_of = None
    try:
        as_of = datetime.fromtimestamp(parquet.stat().st_mtime).isoformat()
    except OSError:
        pass

    # Overall Penang — all plants combined.
    overall = _section("Overall Penang", _agg_with_plant(df), top)

    # Regions — Batu Kawan vs Penang Island (each aggregates its plants).
    regions = []
    for name, plant_codes in _REGIONS:
        sub = df[df["plant"].isin(plant_codes)]
        if not sub.empty:
            regions.append(_section(name, _agg_with_plant(sub), top))

    # Top plants by units.
    order = df.groupby("plant")["units"].sum().sort_values(ascending=False).head(plants).index
    out = [_section(p, df[df["plant"] == p], top) for p in order]

    return {"as_of": as_of, "overall": overall, "regions": regions, "plants": out}


@router.get("/runners")
def ebuild_runners(
    customer: str | None = Query(None, description="Filter to one customer (workcell)."),
    order:    str = Query("top", pattern="^(top|bottom)$", description="'top' = most units first, 'bottom' = least."),
    limit:    int | None = Query(None, ge=1, le=1000, description="Max rows to return (default all)."),
    mode:     str = Query("historical", pattern="^(historical|projection|planner)$",
                          description="historical = units built (24mo); projection = MES demand (~4wk); planner = Excel demand (~13wk)."),
):
    """
    Ranked runners per (SMT_Assembly, Customer). `units` = built (historical) or
    demand (projection/planner). Powers the workcell Report tab's mode toggle.

      → { "as_of": iso|null, "count": N, "runners": [
            { rank, customer, assembly, units, jobs, last_completed,
              first_start, planned_finish }, ... ] }
    """
    parquet = {"projection": PROJECTION_RUNNERS_PARQUET,
               "planner": PLANNER_RUNNERS_PARQUET}.get(mode, RUNNERS_PARQUET)
    if not parquet.exists():
        raise HTTPException(status_code=503, detail=f"{mode} runners mart not built.")

    df = _read_runners(parquet)
    if customer:
        df = df[df["customer"].astype(str).str.casefold() == customer.casefold()]

    df = df.sort_values("units", ascending=(order == "bottom")).reset_index(drop=True)
    df.insert(0, "rank", df.index + 1)
    if limit:
        df = df.head(limit)

    as_of = None
    try:
        as_of = datetime.fromtimestamp(parquet.stat().st_mtime).isoformat()
    except OSError:
        pass

    def _iso(v):
        return None if v is None or pd.isna(v) else (v.isoformat() if hasattr(v, "isoformat") else str(v))

    runners = []
    for r in df.to_dict(orient="records"):
        runners.append({
            "rank": int(r["rank"]),
            "customer": r["customer"],
            "assembly": r["assembly"],
            "units": int(r["units"]),
            "jobs": int(r["jobs"]) if pd.notna(r.get("jobs")) else 0,
            "last_completed": _iso(r.get("last_completed")),
            "first_start": _iso(r.get("first_start")),
            "planned_finish": _iso(r.get("planned_finish")),
        })
    return {"as_of": as_of, "count": len(runners), "runners": runners}
