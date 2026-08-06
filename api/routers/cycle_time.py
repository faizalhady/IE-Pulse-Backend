"""
api/routers/cycle_time.py
──────────────────────────
FastAPI router for the Cycle Time module.
Mounted at /api/cycle-time in api/main.py.

Endpoints:
  GET  /api/cycle-time/health        — parquet status check
  POST /api/cycle-time/refresh       — trigger full pipeline (ingest + transform)
  GET  /api/cycle-time/customers     — list of configured customers
  GET  /api/cycle-time/data          — pivoted data (Image 2 layout), with filters
  GET  /api/cycle-time/raw           — raw row-per-process data, with filters
"""

import logging
import math
import re
import threading
from datetime import datetime
from functools import lru_cache
from typing import Optional

import duckdb
import pandas as pd
import pyarrow.parquet as pq
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from core.auth import require_level
from core.mart_cache import mart_key
from modules.cycle_time.config import (
    CT_CUSTOMERS,
    CT_DUCKDB_MEMORY_LIMIT,
    CT_DUCKDB_TEMP_DIR,
    CT_DUCKDB_THREADS,
    CT_MART,
)

log = logging.getLogger(__name__)

# How far apart the chain marts' write times can be before /health calls the
# data out of sync. A healthy run writes them within minutes of each other;
# transform alone takes ~12 min on the server, so the window has to clear that
# comfortably without hiding a genuinely skipped step (which shows as hours).
SYNC_TOLERANCE_H = 2.0

router = APIRouter(prefix="/api/cycle-time", tags=["Cycle Time"])


# ─── Refresh status (in-process, single source of truth) ─────────────────────
# Mutated by the BackgroundTasks worker, read by GET /refresh/status.
_status_lock = threading.Lock()
_refresh_status: dict = {
    "state":            "idle",     # idle | running | success | failed
    "mode":             None,
    "started_at":       None,
    "finished_at":      None,
    "customers_total":  0,
    "customers_done":   0,
    "current_customer": None,
    "last_error":       None,
}


def _set_status(**kwargs) -> None:
    with _status_lock:
        _refresh_status.update(kwargs)


def _get_status_snapshot() -> dict:
    with _status_lock:
        return dict(_refresh_status)


# ─── Helpers (mirrors OLE api/main.py patterns) ───────────────────────────────

def _con():
    con = duckdb.connect()
    # Guardrail: cap a single query's working memory AND give DuckDB a temp dir
    # so it can SPILL to disk past that cap instead of raising OutOfMemoryException
    # (the old config set a limit but no temp dir, so heavy queries 500'd at the
    # ceiling). `preserve_insertion_order=false` lets large sorts/aggregations use
    # far less memory. A normal customer-filtered query uses a fraction of this —
    # the guardrails only kick in for runaway/unfiltered scans, which now degrade
    # to slower-but-successful instead of failing and pressuring the shared process.
    CT_DUCKDB_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET memory_limit='{CT_DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET threads={CT_DUCKDB_THREADS}")
    con.execute(f"SET temp_directory='{CT_DUCKDB_TEMP_DIR.as_posix()}'")
    con.execute("SET preserve_insertion_order=false")
    return con


def _load_parquet(con, key: str, alias: str):
    """Register a CT parquet file as a DuckDB view. Raises 503 if file missing."""
    path = CT_MART[key]
    if not path.exists():
        raise HTTPException(
            status_code=503,
            detail=f"Cycle Time mart file not found: {path.name}. Run /api/cycle-time/refresh first.",
        )
    con.execute(f"CREATE VIEW {alias} AS SELECT * FROM read_parquet('{path}')")


def _df_to_json(df: pd.DataFrame) -> list[dict]:
    """Safely convert DataFrame to JSON-serialisable list — same as OLE helper."""
    records = df.to_dict(orient="records")
    clean = []
    for row in records:
        clean_row = {}
        for k, v in row.items():
            if isinstance(v, float) and math.isnan(v):
                clean_row[k] = None
            elif not isinstance(v, (list, dict)) and pd.isna(v) if hasattr(pd, "isna") else False:
                clean_row[k] = None
            elif hasattr(v, "item"):
                clean_row[k] = v.item()
            elif hasattr(v, "isoformat"):
                clean_row[k] = v.isoformat()
            else:
                clean_row[k] = v
        clean.append(clean_row)
    return clean


def _build_where(clauses: list[str]) -> str:
    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/health")
def ct_health():
    """
    Lightweight readiness probe — safe to poll.

    Tells you: is the service up, are the mart files present, how many rows
    each has, WHEN each was last written, and whether an ingest is running. Row
    counts come from the parquet FOOTER metadata (a few KB), so NO data is
    loaded into memory.

    `freshness.in_sync` answers "is my data current?" without reading a log.
    The pipeline writes each mart as its own file with no rollback, so a step
    failing mid-chain leaves EARLIER marts fresh and LATER ones stale — mixed
    freshness rather than a clean fall-back. Comparing write times across the
    chain is what surfaces that.
    """
    now = datetime.now()
    status = {}
    for key, path in CT_MART.items():
        if path.exists():
            st = path.stat()
            entry = {
                "exists": True,
                "updated": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "age_hours": round((now.timestamp() - st.st_mtime) / 3600, 1),
            }
            try:
                entry["rows"] = pq.ParquetFile(path).metadata.num_rows
            except Exception:
                entry["rows"] = "unknown"
            status[key] = entry
        else:
            status[key] = {"exists": False, "rows": 0, "updated": None, "age_hours": None}

    # The marts the daily pipeline rewrites in order. If they were all written
    # by the same run their timestamps sit within minutes of each other; a gap
    # of hours means a step failed and later marts never got rebuilt.
    CHAIN = ["raw", "pivoted", "assembly_summary", "customer_status"]
    ages = [status[k]["age_hours"] for k in CHAIN
            if k in status and status[k].get("age_hours") is not None]
    spread = round(max(ages) - min(ages), 1) if len(ages) > 1 else 0.0
    freshness = {
        "in_sync": bool(ages) and spread <= SYNC_TOLERANCE_H,
        "spread_hours": spread,          # oldest vs newest in the chain
        "oldest_hours": max(ages) if ages else None,
        "chain": CHAIN,
        "note": ("marts agree - all written by the same run" if bool(ages) and spread <= SYNC_TOLERANCE_H
                 else "marts disagree - a pipeline step likely failed; check logs/cycle-time.log "
                      "for RUN FAILED, then re-run IEPulse-CycleTime-Ingest"),
    }

    return {
        "status":  "ok" if status.get("pivoted", {}).get("exists") else "not_ready",
        "refresh": _get_status_snapshot()["state"],   # idle | running | success | failed
        "freshness": freshness,
        "mart":    status,
        "customers_configured": len(CT_CUSTOMERS),
    }


def _run_ct_pipeline(mode: str) -> None:
    """Background worker: runs ingest + transform. Errors are logged, not raised
    (the HTTP response has already been sent). Updates _refresh_status throughout
    so the FE can poll GET /refresh/status."""
    _set_status(
        state="running",
        mode=mode,
        started_at=datetime.utcnow().isoformat() + "Z",
        finished_at=None,
        customers_total=len(CT_CUSTOMERS),
        customers_done=0,
        current_customer=None,
        last_error=None,
    )
    try:
        from modules.cycle_time.pipeline.ingest           import run as run_ingest
        from modules.cycle_time.pipeline.transform        import run as run_transform
        from modules.cycle_time.pipeline.eff              import run as run_eff
        from modules.cycle_time.pipeline.assembly_summary import run as run_assembly_summary

        log.info(f"Cycle Time pipeline started (mode={mode})")

        def _progress(current_customer, done, total):
            _set_status(current_customer=current_customer,
                        customers_done=done,
                        customers_total=total)

        # 1. ingest → raw.parquet
        if not run_ingest(mode=mode, progress_cb=_progress):
            _set_status(state="failed", finished_at=datetime.utcnow().isoformat() + "Z",
                        last_error="ingest returned False — check server logs")
            log.error("Cycle Time ingest failed - see logs above")
            return
        # 2. transform → pivoted.parquet
        if not run_transform():
            _set_status(state="failed", finished_at=datetime.utcnow().isoformat() + "Z",
                        last_error="transform returned False — check server logs")
            log.error("Cycle Time transform failed - see logs above")
            return
        # 3. eff → eff_by_line.parquet (best-effort enrichment)
        try:
            if not run_eff():
                log.warning("Efficiency build produced no eff_by_line.parquet - continuing with NULL eff.")
        except Exception:
            log.exception("Efficiency build crashed - continuing (non-fatal).")
        # 4. assembly_summary → assembly_summary.parquet (the has-data ground truth)
        if not run_assembly_summary():
            _set_status(state="failed", finished_at=datetime.utcnow().isoformat() + "Z",
                        last_error="assembly_summary returned False — check server logs")
            log.error("Cycle Time assembly_summary failed - see logs above")
            return
        # 5. CHAIN: rebuild the eBuild runner mart so the Plant Runners dashboard
        #    has_data badges reflect the freshly-synced cycle-time data. Non-fatal —
        #    the cycle-time refresh itself already succeeded.
        try:
            from api.routers.ebuild import build_runners_mart, build_projection_runners_mart
            build_runners_mart(24)             # historical (units built, 24mo)
            build_projection_runners_mart()    # projection (planned demand, ~4wk)
            log.info("Chained eBuild runner mart rebuild complete (historical + projection).")
        except Exception:
            log.exception("Chained eBuild runner refresh failed (non-fatal) - run POST /api/ebuild/refresh manually.")

        _set_status(state="success", finished_at=datetime.utcnow().isoformat() + "Z",
                    customers_done=len(CT_CUSTOMERS), current_customer=None)
        log.info(f"Cycle Time pipeline complete (mode={mode})")
    except Exception as e:
        _set_status(state="failed", finished_at=datetime.utcnow().isoformat() + "Z",
                    last_error=str(e))
        log.exception("Cycle Time pipeline crashed")


@router.post("/refresh", status_code=202,
             dependencies=[Depends(require_level("admin"))])
def ct_refresh(
    background_tasks: BackgroundTasks,
    mode: str = Query("incremental", pattern="^(incremental|full)$"),
):
    """
    Trigger the Cycle Time pipeline in the background.
    Returns immediately with 202 Accepted — the pull runs after the response is sent.
    Check /api/cycle-time/health to see when the parquets are updated.

    mode=incremental (default) — only fetch records updated since last run.
    mode=full                  — re-fetch everything from the API.
    """
    if _get_status_snapshot()["state"] == "running":
        raise HTTPException(status_code=409, detail="A refresh is already running. Poll /refresh/status.")
    background_tasks.add_task(_run_ct_pipeline, mode)
    return {"status": "accepted", "message": f"Cycle Time pipeline started (mode={mode}). Poll /refresh/status."}


@router.get("/refresh/status")
def ct_refresh_status():
    """Current refresh state. Poll while state=='running' to show progress in the UI."""
    return _get_status_snapshot()


@router.get("/customers")
def ct_customers():
    """List all configured Penang customers for the Cycle Time module."""
    return CT_CUSTOMERS


@lru_cache(maxsize=4)
def _coverage_compute(_key) -> list:
    """Cached body of /coverage — see _aliases_compute. Called on every load of
    the Cycle Time landing page; measured ~600ms cold and warm before caching."""
    con = _con()
    try:
        _load_parquet(con, "raw", "ct_raw")
        df = con.execute(
            """
            SELECT customer,
                   COUNT(DISTINCT assembly)                                      AS assemblies,
                   COUNT(DISTINCT (assembly || '§' || COALESCE(revision, '')))   AS revisions,
                   MAX(updated_on)                                               AS updated_on
            FROM ct_raw
            GROUP BY customer
            """
        ).df()
        return _df_to_json(df)
    finally:
        con.close()


@router.get("/coverage")
def ct_coverage():
    """
    Per-customer cycle-time data coverage in ONE pass over raw.parquet:
      → [ { "customer": "ASP", "assemblies": 337, "revisions": 412,
            "updated_on": "2026-06-02..." }, ... ]

    `assemblies` = distinct assemblies that actually have cycle-time data
    locally (contrast with the catalog total in /customers).
    `revisions`  = distinct (assembly, revision) pairs — an assembly with 3
    revisions counts as 3. Powers the Workcells league table without a
    per-customer /profile round trip.
    Returns [] when nothing is ingested yet (mart absent).

    Cached on the mart's mtime — a pipeline rewrite invalidates it automatically.
    """
    if not CT_MART["raw"].exists():
        return []
    return _coverage_compute(mart_key(CT_MART["raw"]))


@lru_cache(maxsize=4)
def _customer_status_from_mart(_key) -> list:
    """Read the CustomerStatus snapshot the pipeline writes.

    Cached on the mart's mtime — exact invalidation, so it can never serve data
    older than the file. Previously this called IEDB live on every request
    (3.6s cold, every restart) behind a 5-minute clock-based cache.
    """
    df = pd.read_parquet(CT_MART["customer_status"])
    return _df_to_json(df)


@router.get("/customer-status")
def ct_customer_status(site: str = Query("pen", description="Site code for the IEDB report (e.g. 'pen').")):
    """
    Proxy to the IEDB CustomerStatus report. Per-customer assembly coverage
    (NoOfAssemblies / NoOfAssembliesWithData / Complete %) plus the measurement-
    method breakdown (StopWatch / Most / Estimate).

    Auth (OAuth Bearer) is handled server-side via modules.cycle_time.auth — the
    browser can't call IEDB directly (401 Unauthorized + cross-origin CORS), so
    the FE hits this endpoint instead. One IEDB API call per request.

      → [ { "CustomerDivision": "ARISTANETWORKS / ARISTANETWORKS*",
            "NoOfAssemblies": 7120, "NoOfAssembliesWithData": 6509, "Complete": 91,
            "StopWatch": 114126, "Most": 0, "Estimate": 2376,
            "EstimatePercentage": 2, "Site": "PEN" }, ... ]
    """
    # Served from the mart snapshot the pipeline writes. Falls back to a live
    # IEDB call only when the snapshot doesn't exist yet (first deploy, before
    # the first pipeline run) — so nothing breaks in the gap.
    mart = CT_MART["customer_status"]
    if mart.exists():
        return _customer_status_from_mart(mart_key(mart))

    log.info("customer_status mart not built yet - falling back to a live IEDB call")
    try:
        from modules.cycle_time.client import fetch_customer_status
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"IEDB client unavailable: {e}")

    try:
        return fetch_customer_status(site=site)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.exception("CustomerStatus fetch failed")
        raise HTTPException(status_code=502, detail=f"IEDB call failed: {e}")


@router.get("/runners")
def ct_runners(
    customer: str = Query(..., description="Customer/workcell name."),
    order:    str = Query("top", pattern="^(top|bottom)$"),
    limit:    Optional[int] = Query(None, ge=1, le=1000),
    mode:     str = Query("historical", pattern="^(historical|projection|planner)$"),
):
    """
    Runner ranking (units built per assembly) for one workcell — thin re-export
    of the eBuild runners mart so the Cycle Time frontend can reach it through
    the existing /cycle-time proxy (the mart itself is owned by the eBuild
    module; see api/routers/ebuild.py). Powers runner-priority + badges on the
    Incompletion Report.
    """
    from api.routers.ebuild import ebuild_runners
    return ebuild_runners(customer=customer, order=order, limit=limit, mode=mode)


@router.get("/customer-plants")
def ct_customer_plants():
    """Dominant plant per customer — re-export of the eBuild customer-plant mart
    so the Cycle Time league table can show a Plant column."""
    from api.routers.ebuild import ebuild_customer_plants
    return ebuild_customer_plants()


@router.get("/plant-runners")
def ct_plant_runners(
    top: int = Query(50, ge=1, le=500), plants: int = Query(3, ge=1, le=20),
    mode: str = Query("historical", pattern="^(historical|projection|planner)$"),
):
    """Plant runner dashboard — re-export of the eBuild plant-runners mart so the
    Cycle Time frontend reaches it through the /cycle-time proxy.
    mode=historical (24mo units built) | projection (~4wk MES demand) | planner (~13wk Excel demand)."""
    from api.routers.ebuild import ebuild_plant_runners
    return ebuild_plant_runners(top=top, plants=plants, mode=mode)


# ─── Assembly catalogue (all assemblies + has_data flag) ──────────────────────
# Refresh state (in-process), mirrors the eBuild refresh state.
_CATALOG_STATE: dict = {"status": "idle", "started": None, "finished": None, "rows": None, "error": None}


def build_assembly_catalog() -> int:
    """
    Pull the FULL IEDB assembly catalogue for every configured customer and
    write assembly_catalog.parquet — one row per (customer, assembly) with a
    `has_data` flag (contrast assembly_summary, which only holds with-data
    assemblies). Fast: two /api/Assemblies calls per customer, no heavy ingest.
    Per-customer failures are logged and skipped. Returns total row count.
    """
    from modules.cycle_time.client import fetch_assemblies

    rows: list[dict] = []
    for c in CT_CUSTOMERS:
        cust, div = c["customer"], c.get("division", "")
        try:
            full = fetch_assemblies(cust, div, has_raw_data=None)
            with_data = fetch_assemblies(cust, div, has_raw_data=True)
        except Exception as e:
            log.warning("assembly catalog: skipping %s (%s)", cust, e)
            continue
        with_ids = {a.get("AssemblyId") for a in with_data}
        for a in full:
            rows.append({
                "customer":     cust,
                "assembly_id":  a.get("AssemblyId"),
                "assembly":     a.get("AssemblyName"),
                "assembly_full": a.get("Assembly"),
                "revision":     a.get("AssemblyRevision"),
                "description":  a.get("AssemblyDescription"),
                "family":       a.get("CustomerFamily"),
                "updated_on":   a.get("UpdatedOn"),
                "has_data":     a.get("AssemblyId") in with_ids,
            })

    cols = ["customer", "assembly_id", "assembly", "assembly_full", "revision",
            "description", "family", "updated_on", "has_data"]
    df = pd.DataFrame(rows, columns=cols)
    CT_MART["assembly_catalog"].parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CT_MART["assembly_catalog"], index=False)
    log.info("assembly catalog: wrote %d rows across %d customers",
             len(df), df["customer"].nunique() if len(df) else 0)
    return len(df)


def _run_catalog_refresh():
    _CATALOG_STATE.update(status="running", started=datetime.now().isoformat(), finished=None, rows=None, error=None)
    try:
        n = build_assembly_catalog()
        _CATALOG_STATE.update(status="success", finished=datetime.now().isoformat(), rows=n)
    except Exception as e:
        log.exception("assembly catalog refresh failed")
        _CATALOG_STATE.update(status="error", finished=datetime.now().isoformat(), error=str(e))


@router.post("/catalog/refresh", dependencies=[Depends(require_level("admin"))])
def ct_catalog_refresh(background: BackgroundTasks):
    """Rebuild assembly_catalog.parquet in the background. Poll GET /catalog/status."""
    if _CATALOG_STATE["status"] == "running":
        return {"status": "running", "detail": "A catalog refresh is already in progress."}
    background.add_task(_run_catalog_refresh)
    return {"status": "started"}


@router.get("/catalog/status")
def ct_catalog_status():
    return _CATALOG_STATE


@router.get("/assembly-catalog")
def ct_assembly_catalog(customer: str = Query(..., description="Customer name — must match a /customers entry.")):
    """
    Per-customer set of assemblies that HAVE cycle-time data, from
    assembly_summary.parquet — the SAME source the Cycle Time tab renders (built
    from the GetDetailRawProcessData ingest). This is the reliable ground truth:
    IEDB's /api/Assemblies is incomplete (misses assemblies that actually have
    data), which gave false "not in IEDB" badges. The Incompletion Report badges
    a runner "Has data" iff its assembly is in this set, else "No data".

      → { "customer": "ARISTANETWORKS", "with_data": ["PCA-01822-11", ...] }
    """
    match = next((c for c in CT_CUSTOMERS if c["customer"] == customer), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"Unknown customer '{customer}'.")

    summ = CT_MART["assembly_summary"]
    if not summ.exists():
        raise HTTPException(status_code=503, detail="assembly_summary mart not found. Run /api/cycle-time/refresh first.")

    df = pd.read_parquet(summ, columns=["customer", "assembly"])
    with_data = sorted({str(x) for x in df.loc[df["customer"] == customer, "assembly"].dropna()})
    return {"customer": customer, "with_data": with_data}


@router.get("/no-data-assemblies")
def ct_no_data_assemblies(customer: str = Query(..., description="Customer name — must match a /customers entry.")):
    """
    List the assemblies for one customer that have NO cycle-time data yet.

    Reads the stored assembly_catalog.parquet (fast, no live call) when present;
    falls back to two live IEDB /api/Assemblies calls (full − with-data) when the
    catalog hasn't been built yet.

      → { "customer": "WABTEC", "total": 394, "with_data": 378, "no_data": 16,
          "assemblies": [ { Assembly, AssemblyName, AssemblyRevision,
                            AssemblyDescription, CustomerFamily, UpdatedOn }, ... ] }
    """
    match = next((c for c in CT_CUSTOMERS if c["customer"] == customer), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"Unknown customer '{customer}'.")

    # Fast path — stored catalog.
    cat = CT_MART["assembly_catalog"]
    if cat.exists():
        df = pd.read_parquet(cat)
        df = df[df["customer"] == customer]
        if len(df):
            nd = df[~df["has_data"].astype(bool)]
            keepmap = {"assembly_full": "Assembly", "assembly": "AssemblyName",
                       "revision": "AssemblyRevision", "description": "AssemblyDescription",
                       "family": "CustomerFamily", "updated_on": "UpdatedOn"}
            recs = nd[list(keepmap)].rename(columns=keepmap).to_dict(orient="records")
            assemblies = [{k: (None if (not isinstance(v, (list, dict)) and pd.isna(v)) else v) for k, v in r.items()} for r in recs]
            return {
                "customer":  customer,
                "total":     int(len(df)),
                "with_data": int(df["has_data"].astype(bool).sum()),
                "no_data":   int(len(nd)),
                "assemblies": assemblies,
            }
        # customer absent from catalog → fall through to live

    # Live fallback.
    try:
        from modules.cycle_time.client import fetch_assemblies
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"IEDB client unavailable: {e}")
    division = match.get("division", "")
    try:
        full = fetch_assemblies(customer, division, has_raw_data=None)
        with_data = fetch_assemblies(customer, division, has_raw_data=True)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.exception("no-data-assemblies fetch failed")
        raise HTTPException(status_code=502, detail=f"IEDB call failed: {e}")

    with_ids = {a.get("AssemblyId") for a in with_data}
    no_data = [a for a in full if a.get("AssemblyId") not in with_ids]
    keep = ("Assembly", "AssemblyName", "AssemblyRevision", "AssemblyDescription",
            "CustomerFamily", "UpdatedOn")
    return {
        "customer":   customer,
        "total":      len(full),
        "with_data":  len(with_data),
        "no_data":    len(no_data),
        "assemblies": [{k: a.get(k) for k in keep} for a in no_data],
    }


@router.get("/live")
def ct_live(
    customer:        str = Query(..., description="Customer name (case-sensitive — must match /customers entry)"),
    page:            int = Query(1,   ge=1, description="IEDB page number"),
    page_size:       int = Query(500, ge=50, le=2000, description="IEDB rows per page"),
    sub_workcenter:  Optional[str] = Query(None, description="Optional line filter"),
):
    """
    Live proxy to IEDB — pivots ONE API page on-the-fly into the same row shape
    as /data, without touching the local parquet mart. Each call burns one
    IEDB API call.

      → {
          "page":        1,
          "page_size":   500,
          "total_count": 9708,
          "pages":       20,
          "has_next":    true,
          "rows":        [...pivoted rows...],
          "alias_map":   { "MA 1": { "processes": [...], "lines": [...] }, ... },
          "note":        "Assemblies spanning a page boundary may appear with partial process columns."
        }

    NOTE: pivot is page-local. An assembly whose processes span pages will
    appear in both pages, each with a subset of process columns filled.
    For complete pivoting use the DB mode (/data) after a successful ingest.
    """
    # Look up the IEDB Division for this customer (required for the API).
    cust_cfg = next((c for c in CT_CUSTOMERS if c["customer"].lower() == customer.lower()), None)
    if cust_cfg is None:
        raise HTTPException(
            status_code=404,
            detail=f"Customer '{customer}' is not configured. See /api/cycle-time/customers.",
        )

    # Defer the import so the router doesn't fail to import if requests isn't installed yet.
    try:
        from modules.cycle_time.client import fetch_page
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Live client unavailable: {e}")

    try:
        batch = fetch_page(
            customer  = cust_cfg["customer"],
            division  = cust_cfg["division"],
            page      = page,
            page_size = page_size,
        )
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.exception("Live fetch failed")
        raise HTTPException(status_code=502, detail=f"IEDB call failed: {e}")

    if not batch:
        return {
            "page":        page,
            "page_size":   page_size,
            "total_count": 0,
            "pages":       0,
            "has_next":    False,
            "rows":        [],
            "alias_map":   {},
            "note":        "No rows for this page.",
        }

    # Normalise to snake_case (same regex as ingest.py — handles PascalCase too).
    import re
    def _snake(name: str) -> str:
        s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
        return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()

    df = pd.DataFrame(batch)
    df.columns = [_snake(c) for c in df.columns]

    # Optional sub_workcenter filter (case-sensitive — matches IEDB).
    if sub_workcenter and "sub_workcenter" in df.columns:
        df = df[df["sub_workcenter"] == sub_workcenter]

    # Pull total_count from first row (IEDB embeds it on every record).
    total_count = int(df["total_count"].iloc[0]) if "total_count" in df.columns and len(df) else 0
    pages = -(-total_count // page_size) if total_count > 0 else 0  # ceiling div

    # Pivot: alias → cycle_time_per_process, index = identity cols.
    index_cols = [c for c in [
        "customer", "division", "family", "assembly", "revision",
        "workcenter", "workcenter_type", "sub_workcenter",
    ] if c in df.columns]

    df["cycle_time_per_process"] = pd.to_numeric(df.get("cycle_time_per_process"), errors="coerce")
    if "alias" not in df.columns:
        # Fallback: if alias is missing, use process. Shouldn't happen for IEDB but safe.
        df["alias"] = df.get("process", "(no name)")
    df["alias"] = df["alias"].fillna("(no alias)")

    pivoted = (
        df.pivot_table(
            index=index_cols,
            columns="alias",
            values="cycle_time_per_process",
            aggfunc="first",
        )
        .reset_index()
    )
    pivoted.columns.name = None
    # Drop alias columns that ended up entirely null after the per-page pivot —
    # same rationale as /data.
    pivoted = pivoted.dropna(axis=1, how="all")

    # Build alias_map from this page's rows.
    alias_map: dict[str, dict[str, list[str]]] = {}
    if "process" in df.columns:
        for alias, sub in df.dropna(subset=["alias"]).groupby("alias"):
            procs = sorted(sub["process"].dropna().unique().tolist())
            lines = sorted(sub["sub_workcenter"].dropna().unique().tolist()) \
                if "sub_workcenter" in df.columns else []
            alias_map[str(alias)] = {"processes": procs, "lines": lines}

    return {
        "page":        page,
        "page_size":   page_size,
        "total_count": total_count,
        "pages":       pages,
        "has_next":    page < pages,
        "rows":        _df_to_json(pivoted),
        "alias_map":   alias_map,
        "note":        "Per-page pivot. Assemblies spanning a page boundary may appear with partial process columns.",
    }


@lru_cache(maxsize=32)
def _aliases_compute(customer: Optional[str], _key) -> dict:
    """Cached body of /aliases. `_key` is the mart mtime — unused inside, it
    exists only so a rewritten raw.parquet produces a different cache key.

    Worth caching: this scans 4 columns x 4.4M rows into pandas and groups them
    in Python. Measured at 12s on the server, cold AND warm, for a 136 KB answer
    that changes once or twice a day. See core/mart_cache.
    """
    con = _con()
    try:
        _load_parquet(con, "raw", "ct_raw")
        where = f"WHERE customer = '{customer}'" if customer else ""
        # `order` is a SQL keyword → quote it. We carry it so the FE can sort the
        # wide-table process columns by physical flow (min order per alias).
        df = con.execute(
            f"""
            SELECT alias, process, sub_workcenter, "order" AS step_order
            FROM ct_raw
            {where}
            """
        ).df()
    finally:
        con.close()

    out: dict[str, dict] = {}
    if df.empty:
        return out
    for alias, sub in df.dropna(subset=["alias"]).groupby("alias"):
        procs = sorted(sub["process"].dropna().unique().tolist())
        lines = sorted(sub["sub_workcenter"].dropna().unique().tolist())
        # Canonical sequence position for this alias = its earliest order seen.
        order = sub["step_order"].dropna()
        out[str(alias)] = {
            "processes": procs,
            "lines": lines,
            "order": int(order.min()) if not order.empty else None,
        }
    return out


@router.get("/aliases")
def ct_aliases(customer: Optional[str] = Query(None, description="Scope by customer")):
    """
    For each distinct `alias` (the customer-facing process name pivoted into
    column headers), return the underlying `process` code(s) and a sample of
    the lines on which it appears.

    Used by the FE to render Process info in the column-header tooltip
    when the table is pivoted on Alias.

      → { "MA 1": { "processes": ["Assembly 1"], "lines": ["ASP HLA ENDO P1B-2", ...] }, ... }

    Cached on the mart's mtime — a pipeline rewrite invalidates it automatically,
    so this can never serve data older than the file on disk.
    """
    if not CT_MART["raw"].exists():
        raise HTTPException(
            status_code=503,
            detail="Cycle Time raw.parquet not found. Run /api/cycle-time/refresh first.",
        )
    return _aliases_compute(customer, mart_key(CT_MART["raw"]))


@router.get("/profile")
def ct_profile(
    customer: str = Query(..., description="Workcell/customer name — must match a /customers entry"),
    pareto_limit: int = Query(20, ge=5, le=60, description="Max processes in the Pareto"),
    top_limit:    int = Query(10, ge=5, le=50, description="Max assemblies in the 'longest builds' list"),
):
    """
    Workcell profile — the analytical 'story' for one customer, computed from raw.parquet.

    A *build* is one (assembly, revision, sub_workcenter) — the unit that has a
    coherent ordered process routing and a meaningful total cycle time. We never
    average cycle time across unlike assemblies (that's noise); instead the headline
    is the **bottleneck**: which process constrains the most builds.

      → {
          "customer": "ASP",
          "summary": { assemblies, builds, lines, processes, revisions, avg_fpy,
                       updated_on, bottleneck: {alias, process, builds_bottlenecked, total_builds, pct} },
          "bottleneck_pareto": [ {alias, process, builds_bottlenecked, pct}, ... ],
          "process_pareto":    [ {alias, process, occurrences, avg_seconds, total_seconds, avg_hc}, ... ],
          "lines":             [ {sub_workcenter, builds, assemblies, avg_build_seconds, total_hc}, ... ],
          "top_assemblies":    [ {assembly, revision, sub_workcenter, total_seconds, n_processes,
                                  total_hc, avg_fpy, bottleneck_alias}, ... ]
        }
    """
    if not CT_MART["raw"].exists():
        raise HTTPException(
            status_code=503,
            detail="Cycle Time raw.parquet not found. Run /api/cycle-time/refresh first.",
        )

    con = _con()
    try:
        _load_parquet(con, "raw", "ct_raw")

        # Guard: does this customer have any rows?
        n = con.execute("SELECT COUNT(*) FROM ct_raw WHERE customer = ?", [customer]).fetchone()[0]
        if n == 0:
            raise HTTPException(
                status_code=404,
                detail=f"No cycle-time data for customer '{customer}'. "
                       f"It may not be ingested yet, or has no cycle times entered in IEDB.",
            )

        # A 'build key' groups one assembly+revision+line — the routing unit.
        BUILD = "assembly || '' || revision || '' || sub_workcenter"

        # ── Summary counts ────────────────────────────────────────────────────
        summary = con.execute(
            f"""
            SELECT
              COUNT(DISTINCT assembly)              AS assemblies,
              COUNT(DISTINCT sub_workcenter)        AS lines,
              COUNT(DISTINCT alias)                 AS processes,
              COUNT(DISTINCT revision)              AS revisions,
              COUNT(DISTINCT ({BUILD}))             AS builds,
              AVG(fpy)                              AS avg_fpy,
              MAX(updated_on)                       AS updated_on
            FROM ct_raw WHERE customer = ?
            """,
            [customer],
        ).df()
        summary_row = _df_to_json(summary)[0]
        total_builds = int(summary_row["builds"] or 0)

        # ── Bottleneck per build → the hero insight ───────────────────────────
        # For each build, the process with the largest cycle time is its bottleneck.
        # Count how often each process is the bottleneck across all builds.
        bottleneck_df = con.execute(
            f"""
            WITH ranked AS (
              SELECT alias, process,
                     ROW_NUMBER() OVER (
                       PARTITION BY {BUILD}
                       ORDER BY cycle_time_per_process DESC NULLS LAST
                     ) AS rn
              FROM ct_raw WHERE customer = ?
            )
            SELECT alias,
                   ANY_VALUE(process)  AS process,
                   COUNT(*)            AS builds_bottlenecked
            FROM ranked WHERE rn = 1
            GROUP BY alias
            ORDER BY builds_bottlenecked DESC
            """,
            [customer],
        ).df()
        bottleneck_rows = _df_to_json(bottleneck_df)
        for r in bottleneck_rows:
            r["pct"] = round(100 * (r["builds_bottlenecked"] or 0) / total_builds, 1) if total_builds else 0.0

        hero = None
        if bottleneck_rows:
            top = bottleneck_rows[0]
            hero = {
                "alias":               top["alias"],
                "process":             top["process"],
                "builds_bottlenecked": top["builds_bottlenecked"],
                "total_builds":        total_builds,
                "pct":                 top["pct"],
            }
        summary_row["bottleneck"] = hero

        # ── Process Pareto (by total time contribution) ───────────────────────
        process_pareto = _df_to_json(con.execute(
            """
            SELECT alias,
                   ANY_VALUE(process)            AS process,
                   COUNT(*)                      AS occurrences,
                   AVG(cycle_time_per_process)   AS avg_seconds,
                   SUM(cycle_time_per_process)   AS total_seconds,
                   AVG(hc)                       AS avg_hc
            FROM ct_raw WHERE customer = ?
            GROUP BY alias
            ORDER BY total_seconds DESC NULLS LAST
            LIMIT ?
            """,
            [customer, pareto_limit],
        ).df())

        # ── Lines (sub_workcenter) summary ────────────────────────────────────
        lines = _df_to_json(con.execute(
            f"""
            WITH build_tot AS (
              SELECT sub_workcenter, assembly, revision,
                     SUM(cycle_time_per_process) AS tot,
                     SUM(hc)                     AS hc
              FROM ct_raw WHERE customer = ?
              GROUP BY sub_workcenter, assembly, revision
            )
            SELECT sub_workcenter,
                   COUNT(*)                 AS builds,
                   COUNT(DISTINCT assembly) AS assemblies,
                   AVG(tot)                 AS avg_build_seconds,
                   AVG(hc)                  AS avg_build_hc
            FROM build_tot
            GROUP BY sub_workcenter
            ORDER BY builds DESC
            """,
            [customer],
        ).df())

        # ── Top assemblies by total build time (with their bottleneck) ────────
        top_assemblies = _df_to_json(con.execute(
            f"""
            WITH per_proc AS (
              SELECT assembly, revision, sub_workcenter, alias, fpy, hc, cycle_time_per_process,
                     ROW_NUMBER() OVER (
                       PARTITION BY {BUILD}
                       ORDER BY cycle_time_per_process DESC NULLS LAST
                     ) AS rn
              FROM ct_raw WHERE customer = ?
            ),
            agg AS (
              SELECT assembly, revision, sub_workcenter,
                     SUM(cycle_time_per_process) AS total_seconds,
                     COUNT(*)                    AS n_processes,
                     SUM(hc)                     AS total_hc,
                     AVG(fpy)                    AS avg_fpy,
                     MAX(CASE WHEN rn = 1 THEN alias END) AS bottleneck_alias
              FROM per_proc
              GROUP BY assembly, revision, sub_workcenter
            )
            SELECT * FROM agg
            ORDER BY total_seconds DESC NULLS LAST
            LIMIT ?
            """,
            [customer, top_limit],
        ).df())

        return {
            "customer":          customer,
            "summary":           summary_row,
            "bottleneck_pareto": bottleneck_rows[:pareto_limit],
            "process_pareto":    process_pareto,
            "lines":             lines,
            "top_assemblies":    top_assemblies,
        }
    finally:
        con.close()


_PIVOT_META_COLS = [
    "customer", "division", "family", "assembly", "revision",
    "workcenter", "workcenter_type", "sub_workcenter", "priority",
]


def _customer_process_columns(con, customer: str) -> list[str]:
    """
    The process columns that actually exist for one customer in pivoted.parquet.
    The pivot names columns by COALESCE(alias, process), so the customer's
    distinct alias-or-process values from raw == its non-null pivot columns.
    Cheap (scans raw, distinct) and lets paginated /data ship a STABLE column
    set across every page (vs per-page dropna, which would shift columns).
    """
    _load_parquet(con, "raw", "ct_raw")
    rows = con.execute(
        "SELECT DISTINCT COALESCE(alias, process) AS c FROM ct_raw "
        "WHERE customer = ? AND COALESCE(alias, process) IS NOT NULL",
        [customer],
    ).fetchall()
    return [r[0] for r in rows]


@router.get("/data")
def ct_data(
    customer:      Optional[str] = Query(None, description="Filter by customer name"),
    assembly:      Optional[str] = Query(None, description="Filter by assembly number (partial match)"),
    revision:      Optional[str] = Query(None, description="Filter by revision"),
    workcenter:    Optional[str] = Query(None, description="Filter by workcenter (e.g. SMT)"),
    sub_workcenter: Optional[str] = Query(None, description="Filter by sub-workcenter"),
    family:        Optional[str] = Query(None, description="Filter by product family"),
    page:          Optional[int] = Query(None, ge=1, description="1-based page. Omit for the full (unpaginated) array."),
    page_size:     int = Query(300, ge=1, le=2000, description="Rows per page when paginating."),
):
    """
    Returns pivoted Cycle Time data — one row per assembly/revision/sub_workcenter,
    process steps (BIRTH, SCRB, GLUEB, …) as columns. The Image 2 table layout.

    Two modes:
      • page omitted → legacy full array (used by Excel export). Trims all-null
        columns via dropna.
      • page set     → paginated envelope { page, page_size, total, pages,
        has_next, columns, rows }. First page paints almost instantly; the FE
        infinite-scrolls the rest. Columns are the customer's stable set so they
        don't shift between pages.
    """
    con = _con()
    try:
        _load_parquet(con, "pivoted", "ct_pivoted")

        clauses = []
        if customer:       clauses.append(f"customer = '{customer}'")
        if assembly:       clauses.append(f"assembly ILIKE '%{assembly}%'")
        if revision:       clauses.append(f"revision = '{revision}'")
        if workcenter:     clauses.append(f"workcenter = '{workcenter}'")
        if sub_workcenter: clauses.append(f"sub_workcenter = '{sub_workcenter}'")
        if family:         clauses.append(f"family ILIKE '%{family}%'")
        where = _build_where(clauses)

        # ── Legacy full fetch (export) ────────────────────────────────────────
        if page is None:
            df = con.execute(
                f"SELECT * FROM ct_pivoted {where} ORDER BY customer, assembly, revision"
            ).df()
            df = df.dropna(axis=1, how="all")
            return _df_to_json(df)

        # ── Paginated ─────────────────────────────────────────────────────────
        total  = con.execute(f"SELECT COUNT(*) FROM ct_pivoted {where}").fetchone()[0]
        offset = (page - 1) * page_size

        # Stable, customer-scoped column set (no per-page dropna).
        pivot_cols = list(con.execute("SELECT * FROM ct_pivoted LIMIT 0").df().columns)
        if customer:
            proc_cols = [c for c in _customer_process_columns(con, customer) if c in pivot_cols]
            sel_cols = [c for c in _PIVOT_META_COLS if c in pivot_cols] + proc_cols
            sel_sql = ", ".join('"' + c.replace('"', '""') + '"' for c in sel_cols)
        else:
            proc_cols = [c for c in pivot_cols if c not in _PIVOT_META_COLS]
            sel_sql = "*"

        df = con.execute(
            # Fully-unique sort (incl. sub_workcenter) so OFFSET paging is stable
            # — a non-unique ORDER BY can skip/duplicate rows across pages.
            f"SELECT {sel_sql} FROM ct_pivoted {where} "
            f"ORDER BY customer, assembly, revision, sub_workcenter LIMIT {page_size} OFFSET {offset}"
        ).df()

        return {
            "page":      page,
            "page_size": page_size,
            "total":     total,
            "pages":     -(-total // page_size),   # ceiling division
            "has_next":  offset + len(df) < total,
            "columns":   proc_cols,
            "rows":      _df_to_json(df),
        }
    finally:
        con.close()


@router.get("/assemblies")
def ct_assemblies(
    customer:       str = Query(..., description="Customer name — must match a /customers entry"),
    sub_workcenter: Optional[str] = Query(None, description="Optional line filter — scope builds to one line"),
    assembly:       Optional[str] = Query(None, description="Optional exact assembly — returns just that one (drawer header)"),
):
    """
    Per-assembly cycle-time aggregate for the 'Assembly Analytics' (Breakdown B)
    view — computed server-side in one pass over raw.parquet so the FE never has
    to download the full pivoted dataset just to summarise it.

      → [ { assembly, family, builds, avg_total, min_total, max_total, bottleneck }, ... ]

    A unit flows SMT → TH → BE once. Within a workcenter there can be:
      • distinct operations (e.g. BE = COAT → PACK → POT) — these are different
        steps of the build and MUST be summed; and
      • alternative lines running the SAME operation (same process-set) — these
        must NOT be summed (that multiplies one unit's time by the line count);
        we keep one representative (the max).
    So per (assembly, workcenter): group builds by their process-set signature,
    take the max within each signature (dedupe alt lines), then SUM across the
    distinct signatures. Assembly cycle time = SMT + TH + BE — one unit start to
    finish. Bottleneck = the single slowest step. The per-step waterfall in the
    drawer is fetched on demand via /data?assembly=…

    Pass `sub_workcenter` to scope to one line — only assemblies built on that
    line are returned, with their stats computed from that line's builds.
    """
    if not CT_MART["raw"].exists():
        raise HTTPException(
            status_code=503,
            detail="Cycle Time raw.parquet not found. Run /api/cycle-time/refresh first.",
        )

    # WHERE customer [AND sub_workcenter] [AND assembly] — applied to both CTEs.
    where = "WHERE customer = ?"
    scope = [customer]
    if sub_workcenter:
        where += " AND sub_workcenter = ?"
        scope.append(sub_workcenter)
    if assembly:
        where += " AND assembly = ?"
        scope.append(assembly)

    con = _con()
    try:
        _load_parquet(con, "raw", "ct_raw")
        df = con.execute(
            f"""
            WITH bp AS (
              -- per (build, workcenter): cycle time + a canonical process-set
              -- signature (sorted, so the same op-set always hashes the same).
              SELECT assembly, revision, sub_workcenter, workcenter,
                     SUM(cycle_time_per_process) AS wc_time,
                     STRING_AGG(DISTINCT COALESCE(alias, process), '|'
                                ORDER BY COALESCE(alias, process)) AS procset
              FROM ct_raw {where}
              GROUP BY assembly, revision, sub_workcenter, workcenter
            ),
            rep AS (
              -- alternative lines/revisions running the SAME operation → one rep.
              SELECT assembly, workcenter, procset, MAX(wc_time) AS rep_time
              FROM bp GROUP BY assembly, workcenter, procset
            ),
            wc AS (
              -- sum the DISTINCT operations within each workcenter.
              SELECT assembly, workcenter, SUM(rep_time) AS wc_total
              FROM rep GROUP BY assembly, workcenter
            ),
            asm AS (
              SELECT assembly,
                     SUM(CASE WHEN workcenter = 'SMT' THEN wc_total ELSE 0 END) AS smt,
                     SUM(CASE WHEN workcenter = 'TH'  THEN wc_total ELSE 0 END) AS th,
                     SUM(CASE WHEN workcenter = 'BE'  THEN wc_total ELSE 0 END) AS be
              FROM wc GROUP BY assembly
            ),
            bcount AS (
              SELECT assembly,
                     COUNT(DISTINCT revision || '|' || sub_workcenter) AS builds
              FROM bp GROUP BY assembly
            ),
            meta AS (
              SELECT assembly,
                     ANY_VALUE(family)                                       AS family,
                     arg_max(COALESCE(alias, process), cycle_time_per_process) AS bottleneck
              FROM ct_raw {where}
              GROUP BY assembly
            )
            SELECT a.assembly, m.family, b.builds,
                   (a.smt + a.th + a.be) AS total,
                   a.smt, a.th, a.be, m.bottleneck
            FROM asm a
            JOIN bcount b USING (assembly)
            JOIN meta m   USING (assembly)
            ORDER BY total DESC NULLS LAST
            """,
            scope + scope,
        ).df()
        return _df_to_json(df)
    finally:
        con.close()


@router.get("/assembly-list")
def ct_assembly_list(
    customer:       str = Query(..., description="Customer name — must match a /customers entry"),
    sub_workcenter: Optional[str] = Query(None, description="Optional line filter"),
):
    """
    Lightweight per-assembly LIST for the 'Cycle Time by Assembly' page collapsed
    rows. ONE grouped pass over raw.parquet — no cycle-time math, no string
    aggregation, no second scan (contrast /assemblies which computes SMT/TH/BE
    totals). Returns only what the collapsed row renders: identity + a stage
    footprint. The per-build process detail is fetched on demand via
    /assembly-builds.

    `builds` keeps its original meaning (distinct revision×line) for the existing
    Assemblies tab. `primary_builds` counts only priority-1 routings (distinct
    revision at priority 1 — what the new Flow tab shows by default), and
    `has_alternates` flags assemblies with any priority>1 routing (drives the
    'show alternate routes' toggle).

    `revisions` = distinct revisions for the assembly (shown in the Assemblies
    table column).

      → [ { assembly, family, builds, revisions, primary_builds, has_alternates,
            has_smt, has_th, has_be, smh, eff }, ... ]

    `smh` = Standard Manufacturing Hour (operator content per unit) =
    Σ (IMT + Hand) × (S%/100) over the primary routing, averaged across the
    assembly's priority-1 revisions. S% is the `sampling` column.

    FAST PATH: when no line filter is given (the workcell page's default), this
    reads the precomputed assembly_summary.parquet — a cheap per-assembly file
    instead of a live aggregation over millions of raw rows. The raw-query path
    below is kept for the line-scoped case (filter dropdown) and as a fallback
    when the summary mart hasn't been built yet.
    """
    # ── Fast path: precomputed per-assembly summary mart ──────────────────────
    if not sub_workcenter and CT_MART["assembly_summary"].exists():
        con = _con()
        try:
            _load_parquet(con, "assembly_summary", "ct_asm")
            df = con.execute(
                """
                SELECT assembly, family, builds, revisions, primary_builds,
                       has_alternates, has_smt, has_th, has_be, smh, eff
                FROM ct_asm
                WHERE customer = ?
                ORDER BY assembly
                """,
                [customer],
            ).df()
            return _df_to_json(df)
        finally:
            con.close()

    if not CT_MART["raw"].exists():
        raise HTTPException(
            status_code=503,
            detail="Cycle Time raw.parquet not found. Run /api/cycle-time/refresh first.",
        )

    where = "WHERE customer = ?"
    scope = [customer]
    if sub_workcenter:
        where += " AND sub_workcenter = ?"
        scope.append(sub_workcenter)

    con = _con()
    try:
        _load_parquet(con, "raw", "ct_raw")
        # `smh` (Standard Manufacturing Hour) = the assembly's operator content
        # per unit: SMH = (IMT + Hand) × (S%/100) summed over a build's processes,
        # where S% is the `sampling` column. Computed on the PRIMARY routing
        # (priority = 1) per revision, then averaged across the assembly's primary
        # revisions so the collapsed row carries one representative value.
        df = con.execute(
            f"""
            WITH base AS (
                SELECT * FROM ct_raw {where}
            ),
            smh AS (
                SELECT assembly, AVG(build_smh) AS smh
                FROM (
                    SELECT assembly, revision,
                           SUM((COALESCE(imt, 0) + COALESCE(hand, 0))
                               * (COALESCE(sampling, 100) / 100.0)) AS build_smh
                    FROM base
                    WHERE priority = 1
                    GROUP BY assembly, revision
                )
                GROUP BY assembly
            )
            SELECT base.assembly,
                   ANY_VALUE(base.family)                                  AS family,
                   COUNT(DISTINCT base.revision || '|' || base.sub_workcenter) AS builds,
                   COUNT(DISTINCT base.revision)                           AS revisions,
                   COUNT(DISTINCT base.revision) FILTER (WHERE base.priority = 1) AS primary_builds,
                   BOOL_OR(base.priority > 1)                              AS has_alternates,
                   BOOL_OR(base.workcenter = 'SMT')                        AS has_smt,
                   BOOL_OR(base.workcenter = 'TH')                         AS has_th,
                   BOOL_OR(base.workcenter = 'BE')                         AS has_be,
                   ANY_VALUE(smh.smh)                                      AS smh,
                   CAST(NULL AS DOUBLE)                                    AS eff
            FROM base
            LEFT JOIN smh USING (assembly)
            GROUP BY base.assembly
            ORDER BY base.assembly
            """,
            scope,
        ).df()
        return _df_to_json(df)
    finally:
        con.close()


@router.get("/assembly-builds")
def ct_assembly_builds(
    customer:       str = Query(..., description="Customer name — must match a /customers entry"),
    assembly:       str = Query(..., description="Exact assembly number"),
    sub_workcenter: Optional[str] = Query(None, description="Optional line filter"),
):
    """
    Per-build process detail for ONE assembly — the expanded-row tables on the
    'Cycle Time by Assembly' page. Reads raw.parquet scoped to a single assembly
    (exact match → predicate pushdown), selecting only the columns the FE needs,
    so it's far lighter than the wide /data?assembly= pivoted fetch it replaces.

    Carries `priority` (routing rank — 1 = primary) and `step_order` (the IEDB
    `order` field, the physical step sequence). A complete build = one routing =
    (revision, priority); its steps can span multiple sub_workcenters and are
    sequenced globally by `step_order`. The FE groups these long rows by
    (revision, priority) and orders the steps by `step_order`.

    Also carries the IEDB step-editor columns: `grp` (group standard), `cap`
    (capacity), `n` (sample size), `sampling` (S%, 1–100), and the time
    components `lct`, `mach`, `imt`, `hand`, `pb`, `hc` (lct is sparse). NOTE the
    FE multiplies `seconds` by `n` for the displayed cycle time (per IE convention).

      → [ { revision, priority, sub_workcenter, workcenter, step, seconds,
            step_order, grp, cap, n, sampling, lct, mach, imt, hand, pb, hc,
            fpy, eff }, ... ]   (eff = per-line efficiency, NULL until built)
    """
    if not CT_MART["raw"].exists():
        raise HTTPException(
            status_code=503,
            detail="Cycle Time raw.parquet not found. Run /api/cycle-time/refresh first.",
        )

    # Columns qualified (ct_raw.*) because the eff join below also exposes
    # `customer`/`sub_workcenter`, which would otherwise be ambiguous.
    where = "WHERE ct_raw.customer = ? AND ct_raw.assembly = ?"
    scope = [customer, assembly]
    if sub_workcenter:
        where += " AND ct_raw.sub_workcenter = ?"
        scope.append(sub_workcenter)

    con = _con()
    try:
        _load_parquet(con, "raw", "ct_raw")
        # Efficiency (per line) — joined from eff_by_line when available so the FE
        # can compute real UPH (3600/CT × eff × FPY) instead of an 85% default.
        if CT_MART["eff_by_line"].exists():
            _load_parquet(con, "eff_by_line", "ct_eff")
            eff_join = ("LEFT JOIN ct_eff e "
                        "ON e.customer = ct_raw.customer "
                        "AND e.sub_workcenter = ct_raw.sub_workcenter")
            eff_select = "ANY_VALUE(e.eff) AS eff"
        else:
            eff_join = ""
            eff_select = "CAST(NULL AS DOUBLE) AS eff"
        # Dedupe by (build, step): raw stores one row PER PLAYBOOK (default + each
        # numbered playbook) with the SAME cycle time, so a naive select triples
        # the steps. Collapse to one row per step — matches the pivoted table
        # (which pivots on alias). Cycle time is identical across playbooks, so
        # MAX returns the true value.
        # `order` is a SQL keyword → must be quoted. step_order = the step's
        # physical sequence (1:1 with the step within a routing). priority is in
        # the GROUP BY so each routing (1 = primary, 2+ = alternates) stays
        # separate even when steps share a sub_workcenter.
        df = con.execute(
            f"""
            SELECT ct_raw.revision, ct_raw.priority, ct_raw.sub_workcenter, ct_raw.workcenter,
                   COALESCE(ct_raw.alias, ct_raw.process)  AS step,
                   MAX(ct_raw.cycle_time_per_process)      AS seconds,
                   MIN(ct_raw."order")                     AS step_order,
                   MAX(ct_raw.cap)                         AS cap,
                   MAX(ct_raw.n)                           AS n,
                   MAX(ct_raw.sampling)                    AS sampling,
                   MAX(ct_raw.grp)                         AS grp,
                   MAX(ct_raw.lct)                         AS lct,
                   MAX(ct_raw.mach)                        AS mach,
                   MAX(ct_raw.imt)                         AS imt,
                   MAX(ct_raw.hand)                        AS hand,
                   MAX(ct_raw.pb)                          AS pb,
                   MAX(ct_raw.hc)                          AS hc,
                   MAX(ct_raw.fpy)                         AS fpy,
                   {eff_select}
            FROM ct_raw {eff_join} {where}
            GROUP BY ct_raw.revision, ct_raw.priority, ct_raw.sub_workcenter, ct_raw.workcenter,
                     COALESCE(ct_raw.alias, ct_raw.process)
            ORDER BY ct_raw.revision, ct_raw.priority, MIN(ct_raw."order")
            """,
            scope,
        ).df()
        return _df_to_json(df)
    finally:
        con.close()


@router.get("/raw")
def ct_raw(
    customer:       Optional[str] = Query(None),
    assembly:       Optional[str] = Query(None),
    revision:       Optional[str] = Query(None),
    workcenter:     Optional[str] = Query(None),
    sub_workcenter: Optional[str] = Query(None),
    process:        Optional[str] = Query(None, description="Filter by process name (e.g. BIRTH, SCRB)"),
    page:           int = Query(1, ge=1),
    page_size:      int = Query(500, ge=1, le=2000),
):
    """
    Returns raw Cycle Time data — one row per (assembly, revision, sub_workcenter, process).
    Paginated. Use /data for the pivoted (Image 2) view.
    """
    con = _con()
    try:
        _load_parquet(con, "raw", "ct_raw")

        clauses = []
        if customer:       clauses.append(f"customer = '{customer}'")
        if assembly:       clauses.append(f"assembly ILIKE '%{assembly}%'")
        if revision:       clauses.append(f"revision = '{revision}'")
        if workcenter:     clauses.append(f"workcenter = '{workcenter}'")
        if sub_workcenter: clauses.append(f"sub_workcenter = '{sub_workcenter}'")
        if process:        clauses.append(f"process = '{process}'")

        where  = _build_where(clauses)
        offset = (page - 1) * page_size

        total = con.execute(f"SELECT COUNT(*) FROM ct_raw {where}").fetchone()[0]
        df    = con.execute(
            f"SELECT * FROM ct_raw {where} ORDER BY customer, assembly, revision, process "
            f"LIMIT {page_size} OFFSET {offset}"
        ).df()

        return {
            "total":     total,
            "page":      page,
            "page_size": page_size,
            "pages":     -(-total // page_size),   # ceiling division
            "data":      _df_to_json(df),
        }
    finally:
        con.close()


# ─── Completion status (IEDB cycle-time vs MES actual route) ──────────────────
# Per top-runner model: unavailable / no_data / incomplete / complete / unverified.
# Heavy job (MES per-customer pull) → background, like the catalog/eBuild refresh.
_COMPLETION_STATE: dict = {"status": "idle", "started": None, "finished": None, "rows": None, "error": None}


def _run_completion_refresh(top_n: int):
    _COMPLETION_STATE.update(status="running", started=datetime.now().isoformat(), finished=None, rows=None, error=None)
    try:
        from modules.cycle_time import completion_status as cs
        df = cs.run(cs.top_models(top_n))
        _COMPLETION_STATE.update(status="success", finished=datetime.now().isoformat(), rows=len(df))
    except Exception as e:
        log.exception("completion-status refresh failed")
        _COMPLETION_STATE.update(status="error", finished=datetime.now().isoformat(), error=str(e))


@router.post("/completion/refresh", dependencies=[Depends(require_level("admin"))])
def ct_completion_refresh(background: BackgroundTasks, top_n: int = Query(100, ge=1, le=500)):
    """Rebuild completion_status + completion_steps marts for the top-`top_n` runner
    union (all layers). Background — poll GET /completion/refresh/status. Needs VPN (MES)."""
    if _COMPLETION_STATE["status"] == "running":
        return {"status": "running", "detail": "A completion refresh is already in progress."}
    background.add_task(_run_completion_refresh, top_n)
    return {"status": "started", "top_n": top_n}


@router.get("/completion/refresh/status")
def ct_completion_refresh_status():
    return _COMPLETION_STATE


@router.get("/completion")
def ct_completion(
    customer: Optional[str] = Query(None, description="Filter to one workcell (case-insensitive)."),
    status:   Optional[str] = Query(None, description="Filter to one status (incomplete/complete/no_data/unavailable/unverified)."),
):
    """Completion-status summary per model. → { as_of, count, counts:{status:n}, models:[...] }."""
    p = CT_MART["completion_status"]
    if not p.exists():
        raise HTTPException(status_code=503, detail="completion_status mart not built. POST /completion/refresh first.")
    df = pd.read_parquet(p)
    # join per-model LBR% + IPK trolleys (line_metrics mart) when available
    lm = CT_MART["line_metrics"]
    if lm.exists():
        m = pd.read_parquet(lm)[["customer", "assembly", "lbr", "ipk_trolleys"]]
        df = df.merge(m, on=["customer", "assembly"], how="left")
    if customer:
        df = df[df["customer"].astype(str).str.casefold() == customer.casefold()]
    if status:
        df = df[df["status"] == status]
    as_of = None
    try:
        as_of = datetime.fromtimestamp(p.stat().st_mtime).isoformat()
    except OSError:
        pass
    return {
        "as_of": as_of,
        "count": len(df),
        "counts": df["status"].value_counts().to_dict() if len(df) else {},
        "models": _df_to_json(df),
    }


# ─── Demand-scoped completion (the Incompletion Report page) ─────────────────
# The plain /completion endpoint lists whatever is in the mart, in no useful
# order. This one answers the question the report actually asks: "of the models
# we are building and about to build, which have complete cycle times?"
#
# Demand = MES projection (SP_GET_SY_SMT_BUILDPLAN, ~4wk forward) UNION planner
# demand (planners' Excel, ~13wk). Neither alone is enough — the Excel covers 18
# workcells, MES covers 39 — so the union is the real scope. Ranked by units,
# because volume is heavily concentrated: the top 500 models are 88% of it.
_DEMAND_MARTS = ("projection_runners.parquet", "planner_runners.parquet")

# MES plant codes -> the grouping the floor actually uses.
_PLANT_REGION = {"JBK": "Batu Kawan", "Plant 1": "Penang Island", "JPE": "Penang Island",
                 "Unassigned": "Other"}


def _canonical_customers() -> dict:
    """normalised name -> the spelling the Cycle Time module uses.

    MES, the planners' Excel and IEDB each spell workcells differently — the
    demand marts contain RESMED *and* ResMed, MASIMO *and* Masimo. Left alone
    they show up as separate workcells in the picker and split one workcell's
    models across two rows. CT_CUSTOMERS is the canonical list, so everything
    collapses onto its spelling; anything it doesn't know keeps the first
    spelling seen.
    """
    return {_cnorm_key(c["customer"]): c["customer"] for c in CT_CUSTOMERS}


def _cnorm_key(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def _demand_frame() -> pd.DataFrame:
    """One row per (customer, assembly): summed demand units + dominant plant.

    A model can be planned in more than one plant (INFINERA runs in both JBK and
    Plant 1), so the plant shown is the one with the most units — same rule
    eBuild uses for customer_plant.
    """
    eb = CT_MART["raw"].parent.parent / "ebuild"
    frames = []
    for name in _DEMAND_MARTS:
        f = eb / name
        if f.exists():
            d = pd.read_parquet(f)
            d["src"] = "mes" if name.startswith("projection") else "planner"
            for c in ("first_start", "planned_finish"):
                if c not in d.columns:
                    d[c] = pd.NaT
            frames.append(d[["plant", "customer", "assembly", "units", "src",
                             "first_start", "planned_finish"]])
    if not frames:
        return pd.DataFrame(columns=["plant", "customer", "assembly", "units", "sources"])

    d = pd.concat(frames, ignore_index=True)
    d["units"] = pd.to_numeric(d["units"], errors="coerce").fillna(0)

    # Collapse spelling variants BEFORE any grouping, or one workcell becomes two.
    canon = _canonical_customers()
    d["_ck"] = d["customer"].map(_cnorm_key)
    first_seen = d.drop_duplicates("_ck").set_index("_ck")["customer"].to_dict()
    d["customer"] = [canon.get(k, first_seen.get(k, c))
                     for k, c in zip(d["_ck"], d["customer"])]
    d = d.drop(columns=["_ck"])
    # A handful of planner rows carry no plant (Cohu). Without this they vanish
    # from the picker while still appearing in the table — a workcell you can
    # see but cannot filter to.
    d["plant"] = d["plant"].fillna("Unassigned")
    # dominant plant = most units for that model
    dom = (d.groupby(["customer", "assembly", "plant"], as_index=False)["units"].sum()
             .sort_values("units", ascending=False)
             .drop_duplicates(["customer", "assembly"])[["customer", "assembly", "plant"]])
    for c in ("first_start", "planned_finish"):
        d[c] = pd.to_datetime(d[c], errors="coerce")
    # "Next build" means the next one — so only starts from today onward count.
    # Plain min(first_start) returned the FIRST start on record, which for 2,847
    # of 3,913 models was a date in the past.
    today = pd.Timestamp.today().normalize()
    d["_upcoming"] = d["first_start"].where(d["first_start"] >= today)
    agg = (d.groupby(["customer", "assembly"], as_index=False)
             .agg(units=("units", "sum"),
                  sources=("src", lambda s: "+".join(sorted(set(s)))),
                  next_build=("_upcoming", "min"),          # next start from today on
                  last_build=("planned_finish", "max")))    # when current demand runs out
    # A model already on the floor has no future start but its demand still runs.
    # Without this it shows a blank Next Build and reads as "not building" — true
    # for 1,266 models today, a third of the report.
    agg["in_progress"] = agg["next_build"].isna() & (agg["last_build"] >= today)
    return agg.merge(dom, on=["customer", "assembly"], how="left")


@lru_cache(maxsize=8)
def _completion_demand(_key) -> dict:
    """Completion status joined to demand, ranked by units. Cached on the mtimes
    of every mart it reads, so any refresh invalidates it exactly."""
    dem = _demand_frame()
    if dem.empty:
        return {"scope": {}, "models": [], "counts": {}, "unchecked": 0}

    st = pd.read_parquet(CT_MART["completion_status_v2"])
    # Case and punctuation differ between MES, the planners' Excel and IEDB
    # ("RESMED" vs "ResMed"), so join on a normalised key, not the raw strings.
    for f in (dem, st):
        f["_k"] = (f["customer"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
                   + "|"
                   + f["assembly"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True))

    keep = [c for c in ("status", "reason", "near_match", "source", "expected", "present", "no_ct",
                        "not_in_iedb", "unmapped", "non_iedb", "actual_steps",
                        "coverage") if c in st.columns]
    # The status mart still carries both spellings of a few workcells (RESMED and
    # ResMed), so one model can have two rows — one judged, one a phantom "absent".
    # Keep the row that actually saw production, or dedupe picks whichever landed
    # first and a complete model reads as not_in_iedb.
    if "actual_steps" in st.columns:
        st = st.sort_values("actual_steps", ascending=False)
    out = dem.merge(st[["_k"] + keep].drop_duplicates("_k"), on="_k", how="left")

    # LBR% and IPK trolleys — the two line-design indicators. Only meaningful
    # once a model's route is complete, so they are frequently null; the table
    # shows a dash rather than pretending zero.
    lm_path = CT_MART["line_metrics"]
    if lm_path.exists():
        lm = pd.read_parquet(lm_path)[["customer", "assembly", "lbr", "ipk_trolleys",
                                       "bottleneck_ct", "station_count"]]
        lm["_k"] = (lm["customer"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
                    + "|"
                    + lm["assembly"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True))
        out = out.merge(lm.drop(columns=["customer", "assembly"]).drop_duplicates("_k"),
                        on="_k", how="left")
    out["status"] = out["status"].fillna("not_checked")
    out = out.sort_values("units", ascending=False).reset_index(drop=True)
    out["rank"] = out.index + 1
    out["region"] = out["plant"].map(_PLANT_REGION).fillna("Other")

    # Each workcell gets ONE home plant in the picker — the one where most of its
    # demand sits. A few (INFINERA) genuinely run in two plants, but listing them
    # twice means un-ticking a plant silently un-ticks the workcell under the
    # other one too, and the plant checkbox lands in a permanent partial state.
    # Filtering is by workcell name anyway, so nothing is lost: picking the
    # workcell still includes every model it has, in whichever plant.
    home = (out.groupby(["customer", "plant"], as_index=False)["units"].sum()
               .sort_values("units", ascending=False)
               .drop_duplicates("customer"))
    by_plant: dict = {}
    for plant, g in home.groupby("plant"):
        by_plant[str(plant)] = sorted(g["customer"].astype(str).unique().tolist())

    return {
        "scope": {
            "plants": by_plant,
            "regions": {r: sorted({p for p, rr in _PLANT_REGION.items() if rr == r} & set(by_plant))
                        for r in sorted(set(_PLANT_REGION.values()))},
            "workcells": sorted(out["customer"].astype(str).unique().tolist()),
        },
        "counts": out["status"].value_counts().to_dict(),
        "unchecked": int((out["status"] == "not_checked").sum()),
        "models": _df_to_json(out.drop(columns=["_k"])),
    }


@router.get("/completion/demand")
def ct_completion_demand(
    plants:    Optional[str] = Query(None, description="Comma-separated plant codes (e.g. 'Plant 1,JBK'). Omit for all."),
    workcells: Optional[str] = Query(None, description="Comma-separated workcells. Takes precedence over `plants`."),
    status:    Optional[str] = Query(None, description="Comma-separated statuses to keep."),
    limit:     int = Query(0, ge=0, le=5000, description="Top N by demand units. 0 = all."),
):
    """Completion status for the models we are actually building and planning.

      → { as_of, count, total, counts:{status:n}, unchecked,
          scope:{ plants:{plant:[workcell]}, regions:{}, workcells:[] },
          models:[ { rank, plant, region, customer, assembly, units, sources,
                     status, source, expected, present, coverage, ... } ] }

    `scope` drives the plant/workcell picker, so the UI never has to know the
    plant layout itself. Rows the completion run has not reached yet come back
    as status "not_checked" rather than being dropped — a model missing from the
    report is indistinguishable from one with no problems otherwise.
    """
    if not CT_MART["completion_status_v2"].exists():
        raise HTTPException(status_code=503,
                            detail="completion_status_v2 mart not built. Run scripts/run_completion_target.py first.")

    eb = CT_MART["raw"].parent.parent / "ebuild"
    data = _completion_demand(mart_key(CT_MART["completion_status_v2"],
                                       *(eb / n for n in _DEMAND_MARTS)))
    rows = data["models"]
    total = len(rows)

    if workcells:
        want = {w.strip().casefold() for w in workcells.split(",") if w.strip()}
        rows = [r for r in rows if str(r.get("customer", "")).casefold() in want]
    elif plants:
        want = {p.strip().casefold() for p in plants.split(",") if p.strip()}
        rows = [r for r in rows if str(r.get("plant", "")).casefold() in want]
    if status:
        want = {s.strip() for s in status.split(",") if s.strip()}
        rows = [r for r in rows if r.get("status") in want]
    if limit:
        rows = rows[:limit]

    as_of = None
    try:
        as_of = datetime.fromtimestamp(CT_MART["completion_status_v2"].stat().st_mtime).isoformat()
    except OSError:
        pass

    return {
        "as_of": as_of,
        "total": total,
        "count": len(rows),
        "counts": pd.Series([r["status"] for r in rows]).value_counts().to_dict() if rows else {},
        "unchecked": data["unchecked"],
        "scope": data["scope"],
        "models": rows,
    }


@router.get("/completion/steps")
def ct_completion_steps(
    customer: str = Query(..., description="Workcell (case-insensitive)."),
    assembly: str = Query(..., description="Model / assembly name."),
):
    """MES actual route vs IEDB route for one model — the FE side-by-side.
      → { customer, assembly, mes:[{order,step,alias,qty,status}], iedb:[{process,alias,sub_workcenter,cycle_time}] }
    mes step status: present | missing | non_iedb | unmapped."""
    # v2 FIRST. The Incompletion Report's badge comes from the v2 mart, so reading
    # v1 here made the drawer disagree with the badge beside it — LIFE360
    # 410-10152-00-Z1 showed a full IEDB route under a "Not in IEDB" chip, because
    # the two panels were quoting different computations. v1 stays as the fallback
    # for models v2 has not reached.
    marts = [CT_MART["completion_steps_v2"], CT_MART["completion_steps"]]
    if not any(m.exists() for m in marts):
        raise HTTPException(status_code=503, detail="completion_steps mart not built. POST /completion/refresh first.")
    slices = []
    for p in marts:
        if not p.exists():
            continue
        d = pd.read_parquet(p)
        # Demand plans carry stray whitespace and tabs in model names — match on
        # the stripped value or the drawer 404s on a model the report just listed.
        d = d[(d["customer"].astype(str).str.casefold() == customer.casefold())
              & (d["assembly"].astype(str).str.strip() == assembly.strip())]
        if not d.empty:
            # The mart carries some workcells under two spellings (MASIMO and
            # Masimo). Matching case-insensitively pulled BOTH copies in, so the
            # drawer drew every step twice — once judged against IEDB, once
            # unmapped. Keep one spelling: the copy that actually has an IEDB
            # route, else the fuller one.
            if d["customer"].nunique() > 1:
                best = max(d["customer"].unique(),
                           key=lambda c: (int((d[(d["customer"] == c)]["side"] == "IEDB").sum()),
                                          int((d["customer"] == c).sum())))
                d = d[d["customer"] == best]
            slices.append(d)
    # Take each side from the first mart that actually HAS it. v2 leads, but the
    # two marts pull MES from different sources, so v2 can hold the IEDB route
    # while only v1 saw production (LIFE360 410-10152-00-Z1). Preferring v2
    # wholesale blanked the MES panel for those.
    def _side(name):
        for d in slices:
            s = d[d["side"] == name]
            if not s.empty:
                return s
        return pd.DataFrame(columns=slices[0].columns) if slices else pd.DataFrame()
    df = pd.concat([_side("MES"), _side("IEDB")]) if slices else pd.DataFrame()
    if not df.empty:
        mes = [{"order": None if pd.isna(r["order"]) else int(r["order"]), "step": r["name"],
                "alias": r["alias"], "qty": None if pd.isna(r["value"]) else int(r["value"]), "status": r["status"]}
               for _, r in df[df["side"] == "MES"].sort_values("order").iterrows()]
        iedb = [{"process": r["name"], "alias": r["alias"], "sub_workcenter": r["sub_workcenter"],
                 "order": None if pd.isna(r["order"]) else int(r["order"]),
                 "cycle_time": None if pd.isna(r["value"]) else float(r["value"])}
                for _, r in df[df["side"] == "IEDB"].sort_values("order", na_position="last").iterrows()]
        return {"customer": customer, "assembly": assembly, "mes": mes, "iedb": iedb}

    # Not in the comparison mart (e.g. Unverified — no MES history). Still show the
    # IEDB route from raw so the drawer isn't blank; MES side is just empty.
    con = duckdb.connect()
    idf = con.execute(f"""
        SELECT DISTINCT process, alias, sub_workcenter, "order" AS ord, cycle_time_per_process AS ct
        FROM read_parquet('{CT_MART["raw"].as_posix()}')
        WHERE lower(customer) = lower(?) AND assembly = ? AND cycle_time_per_process IS NOT NULL AND priority = 1
        ORDER BY "order"
    """, [customer, assembly]).fetchdf()
    con.close()
    if idf.empty:
        # 200 with empty sides, not 404. The drawer's query retried the 404 with
        # backoff, so a model with nothing to show sat on a spinner instead of
        # saying so. "Nothing here" is a result, not an error.
        return {"customer": customer, "assembly": assembly, "mes": [], "iedb": []}
    iedb = [{"process": r["process"], "alias": r["alias"], "sub_workcenter": r["sub_workcenter"],
             "order": None if pd.isna(r["ord"]) else int(r["ord"]),
             "cycle_time": None if pd.isna(r["ct"]) else float(r["ct"])}
            for _, r in idf.iterrows()]
    return {"customer": customer, "assembly": assembly, "mes": [], "iedb": iedb}


@router.get("/completion/line-metrics")
def ct_completion_line_metrics(
    customer: str = Query(..., description="Workcell (config name, e.g. 'Nokia Optics')."),
    assembly: str = Query(..., description="Model / assembly name."),
):
    """Per-model LBR + IPK breakdown from the IEDB route (the drawer proof).
      → { customer, assembly, lbr, n0, bottleneck_ct, ipk_trolleys, boards_per_trolley,
          lines:[{sub_workcenter, lbr, n0, bottleneck_step, bottleneck_ct, balance_line,
                  stations:[{step, ct, is_bottleneck}]}],
          buffers:[{from, to, up_uph, down_uph, gap, ipk_units, trolleys}] }
    Only meaningful for models with COMPLETE cycle-time data."""
    from modules.cycle_time.line_metrics import compute_one
    m = compute_one(customer, assembly)
    if not m:
        raise HTTPException(status_code=404, detail=f"No line metrics for {customer} / {assembly} (no priority-1 cycle-time data).")
    return {"customer": customer, "assembly": assembly, **m}
