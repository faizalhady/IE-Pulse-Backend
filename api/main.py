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
from api.routers.saved_reports import router as saved_reports_router


# Dual console+file logging, faulthandler, and global excepthooks. Done at import
# so even an import-time crash lands in logs/ instead of vanishing.
log = setup_logging()

app = FastAPI(title="IE Pulse API", version="1.0.0")

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
app.include_router(saved_reports_router)


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
        jobs = [
            ("cycle-time/coverage", lambda: ct_router_mod._coverage_compute(mart_key(CT_MART["raw"]))),
            ("cycle-time/aliases",  lambda: ct_router_mod._aliases_compute(None, mart_key(CT_MART["raw"]))),
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
