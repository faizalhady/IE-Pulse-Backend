"""
api/main.py
───────────
FastAPI app entry point. Thin — only wires together middleware, startup
hooks, and module routers. Endpoint logic lives in api/routers/*.

To add a new module:
  1. Build its router in api/routers/<module>.py
  2. Import + include_router below
"""

import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# Load .env from the repo root up front (anchored to this file, NOT the process
# CWD) so IEDB_CLIENT_KEY is present for every module and the startup banner
# reports it accurately — regardless of where Servy launches the process.
from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

import api.routers.cycle_time as ct_router_mod
from core.database import init_db
from core.logging_setup import (
    setup_logging,
    install_signal_logging,
    log_startup_banner,
    log_shutdown_banner,
    start_heartbeat,
    stop_heartbeat,
)
from api.routers.ole         import router as ole_router
from api.routers.downtime    import router as downtime_router
from api.routers.transfers   import router as transfers_router
from api.routers.cycle_time  import router as cycle_time_router
from api.routers.ppqt        import router as ppqt_router
from api.routers.lbr         import router as lbr_router
from api.routers.ipk         import router as ipk_router
from api.routers.ebuild      import router as ebuild_router
from api.routers.access      import router as access_router
from api.routers.saved_reports import router as saved_reports_router
from api.routers.smh         import router as smh_router


# Dual console+file logging, faulthandler, and global excepthooks. Done at import
# so even an import-time crash lands in logs/ instead of vanishing.
log = setup_logging()

app = FastAPI(title="IE Pulse API", version="1.0.0")

# The big list endpoints (SMH's 32k rows, cycle-time raw) are repetitive JSON
# that compresses ~10x. Nothing in front of this process compresses.
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Module routers ───────────────────────────────────────────────────────────
app.include_router(ole_router)
app.include_router(downtime_router)
app.include_router(transfers_router)
app.include_router(cycle_time_router)
app.include_router(ppqt_router)
app.include_router(lbr_router)
app.include_router(ipk_router)
app.include_router(ebuild_router)
app.include_router(access_router)
app.include_router(saved_reports_router)
app.include_router(smh_router)


@app.on_event("startup")
def startup():
    # Re-assert our logging config now that uvicorn has installed its own, then
    # arm the lifecycle forensics so the next "silent" stop is fully traceable.
    setup_logging()
    log_startup_banner(log)
    install_signal_logging(log)          # logs which signal triggers a shutdown
    start_heartbeat(log)                 # 5-min liveness; last one = moment of death
    init_db()
    log.info("SQLite operational DB ready")
    _warm_caches()


def _warm_universe(ct, CT_MART):
    """Warm /universe/summary. Its cache key is the mtimes of six marts, so it
    is built here rather than inlined in the job list."""
    from core.mart_cache import mart_key
    ct_dir, eb = CT_MART["raw"].parent, CT_MART["raw"].parent.parent / "ebuild"
    return ct._universe_summary(mart_key(
        ct_dir / "assembly_catalog.parquet", ct_dir / "raw.parquet",
        ct_dir / "completion_status_v2.parquet", eb / "runners.parquet",
        eb / "projection_runners.parquet", eb / "planner_runners.parquet"))


def _warm_model_universe() -> None:
    """Read the stored universe once so the first model list does not."""
    from modules.cycle_time.model_universe import build
    build()


def _warm_registry() -> None:
    """Both process lists, plus the 11.9 MB raw CSV they share.

    `scanned` and `configured` read the same file but build different frames, so
    warming one does not warm the other. Warmed with the same default sort and
    page size the pages request, or the first visitor still pays for the sort.
    """
    from modules.cycle_time import registry
    registry.workcells()
    registry.process_list(scope="scanned", page_size=300)
    registry.process_list(scope="configured", page_size=300)


def _warm_caches() -> None:
    """Populate the mart caches in the background so the first real visitor
    doesn't pay for them.

    The caches are per-process and empty on boot, so before this the first
    request after every restart paid full price — measured on the server:
    /cycle-time/aliases 12.8s, /coverage 582ms. Everyone after got ~20ms.

    Runs on a daemon thread: warming must never delay startup or stop the app
    coming up if a mart is missing or malformed.
    """
    def _warm():
        from modules.cycle_time.config import CT_MART
        from core.mart_cache import mart_key
        # Everything the Cycle Time landing page calls on load. Keep this list in
        # step with that page - an endpoint missing here is one the first
        # visitor after a restart still pays for.
        jobs = [
            ("cycle-time/coverage", lambda: ct_router_mod._coverage_compute(mart_key(CT_MART["raw"]))),
            ("cycle-time/aliases",  lambda: ct_router_mod._aliases_compute(None, mart_key(CT_MART["raw"]))),
            ("cycle-time/customer-status",
             lambda: ct_router_mod._customer_status_from_mart(mart_key(CT_MART["customer_status"]))
                     if CT_MART["customer_status"].exists() else None),
            # The demand table and the Coverage page. `demand` is warmed as
            # SERIALISED BYTES, not just the frame — the encoding was 0.3s of a
            # 2s response and the first visitor after every restart paid it.
            ("cycle-time/completion-demand",
             lambda: ct_router_mod._completion_demand_json(
                 ct_router_mod._completion_demand_key())),
            ("cycle-time/universe-summary", lambda: _warm_universe(ct_router_mod, CT_MART)),
            # The model universe parquet, 57k rows. Every model list joins it,
            # and it was re-read from disk per request until it was cached.
            ("cycle-time/model-universe", _warm_model_universe),
            # The demand frame behind every model list — two parquets and a
            # groupby, 197ms, previously recomputed on every single request.
            ("cycle-time/demand-frame", lambda: ct_router_mod._demand_frame()),
            # The process registry: the workcell Processes tab and the global
            # Processes page. The raw CSV is 11.9 MB and the configured list is
            # 72,692 couples — 1.1s and 0.7s respectively on a cold process.
            ("cycle-time/registry", _warm_registry),
        ]
        for name, fn in jobs:
            t0 = time.time()
            try:
                fn()
                log.info("cache warmed: %-22s %.1fs", name, time.time() - t0)
            except Exception as e:
                # Never fatal — a cold cache just means the first user waits.
                log.warning("cache warm skipped for %s: %s", name, e)

    threading.Thread(target=_warm, name="cache-warm", daemon=True).start()


@app.on_event("shutdown")
def shutdown():
    # If this banner appears, the stop was a graceful signalled shutdown (not a
    # hard kill). Pair it with the "SIGNAL RECEIVED" line to see who asked.
    stop_heartbeat()
    log_shutdown_banner(log)


if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
