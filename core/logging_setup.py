"""
core/logging_setup.py
─────────────────────
Centralised logging + crash-visibility for the backend.

Why this exists
───────────────
The service has been stopping on the server without leaving a clear cause in
Servy's captured stdout/stderr. A graceful uvicorn shutdown only prints
"Shutting down" — it never says WHO sent the signal or WHY. This module makes
the process narrate its own lifecycle so the next stop is diagnosable:

  • Dual logging  — console (Servy captures it) + a ROTATING file under logs/
    that survives restarts and isn't truncated when Servy recycles its capture.
  • faulthandler  — dumps a native C-level traceback to logs/faulthandler.log on
    a hard fault (segfault, stack overflow). Plain Python logging can't see
    these; they're the classic "silent" death. Fault-time only — the periodic
    all-thread dump was removed 2026-07-27 after it was proven to be crashing
    the process itself (see _install_faulthandler).
  • Excepthooks   — any unhandled exception on the main thread OR a worker thread
    (the refresh pipeline runs in a thread) is logged with a full traceback
    instead of vanishing.
  • Signal logging — logs WHICH signal (SIGINT / SIGTERM / SIGBREAK) triggered a
    shutdown, then chains to uvicorn's own handler so graceful shutdown still
    works. This is the line that finally answers "who stopped it" — a deliberate
    Servy/console stop sends a signal; a hard TerminateProcess does not.
  • Banners + heartbeat — startup banner records pid/cwd/key-loaded; a heartbeat
    line every 5 min records uptime + RSS. The LAST heartbeat before the log goes
    quiet pinpoints the moment of death and whether memory was climbing
    (OOM-kill) vs a clean signalled stop (which logs a shutdown banner).

Console vs file
───────────────
The FILE gets everything. The CONSOLE drops two high-volume INFO streams —
per-request `uvicorn.access` lines and the heartbeat — so a terminal shows
startup, warnings and errors only. Warnings/errors are never filtered. See
_ConsoleNoiseFilter.

Read the forensics like this after a stop:
  - shutdown banner + "SIGNAL RECEIVED" present  → something asked it to stop
    (Servy stop, console close, deploy). Not a crash.
  - heartbeats stop with NO banner, RSS was high  → hard kill / OOM. Check
    Windows Event Log + faulthandler.log.
  - faulthandler.log has a fresh dump                 → native crash / hang.
"""

import faulthandler
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"

MAIN_LOG = "pulse-backend.log"

# Modules that get their own log file. A module's tag is derived from the logger
# name, which is already __name__ everywhere — so this needs no code changes at
# the call sites.
MODULES = ("ole", "cycle-time", "ppqt", "ipk", "lbr")

_FMT = "%(asctime)s  %(levelname)-7s [%(module_tag)-10s] %(short)s: %(message)s"


def _tag_for(name: str) -> str:
    """Map a logger name to a module tag.

        modules.cycle_time.client   -> cycle-time
        api.routers.ole             -> ole
        uvicorn.access              -> http
        anything else               -> core
    """
    parts = name.split(".")
    if name.startswith("modules.") and len(parts) > 1:
        return parts[1].replace("_", "-")
    if name.startswith("api.routers.") and len(parts) > 2:
        return parts[2].replace("_", "-")
    if name.startswith(("uvicorn", "fastapi", "starlette")):
        return "http"
    return "core"


class _ModuleFilter(logging.Filter):
    """Stamps every record with `module_tag` + `short` so the formatter can use
    them, and — when `only` is set — keeps just one module's records.

    Attached to handlers rather than loggers so it also sees records that
    propagate up from uvicorn/fastapi. Stamping is idempotent: a record hitting
    several handlers is only computed once.
    """

    def __init__(self, only: str | None = None):
        super().__init__()
        self.only = only

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "module_tag"):
            record.module_tag = _tag_for(record.name)
            record.short = record.name.rsplit(".", 1)[-1]
        return self.only is None or record.module_tag == self.only

# Module-level so they live for the whole process (don't get GC'd).
_fault_log_handle = None
_prev_signal_handlers: dict = {}
_hb_stop = threading.Event()
_start_time = time.time()


class _ConsoleNoiseFilter(logging.Filter):
    """Keep the terminal readable. The FILE still records everything — this only
    hides the two high-volume INFO streams from the console:

      • uvicorn.access — one line per HTTP request; the dashboard polls a lot.
      • heartbeat      — a liveness line with uptime + RSS. Its whole job is to
                         be the last thing in the FILE before a death, so the
                         file is the only place it matters.

    Warnings and errors from both still come through — this filter only drops
    records below WARNING.
    """

    _MUTED_LOGGERS = ("uvicorn.access",)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        if record.name.startswith(self._MUTED_LOGGERS):
            return False
        return not record.getMessage().startswith("heartbeat")


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Install dual (console + rotating file) logging, faulthandler, and global
    excepthooks. Idempotent — safe to call more than once (it clears handlers
    first, so calling it again after uvicorn configures its own logging wins)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):        # drop basicConfig/uvicorn handlers
        root.removeHandler(h)

    fmt = logging.Formatter(_FMT)

    # Force UTF-8 on the console stream so non-ASCII (— … etc.) isn't mangled to
    # mojibake in Servy's captured stdout (the old prod logs showed "�" for these).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(_ModuleFilter())       # must stamp before the formatter runs
    console.addFilter(_ConsoleNoiseFilter())
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / MAIN_LOG,
        maxBytes=10 * 1024 * 1024,       # 10 MB per file
        backupCount=10,                  # ~100 MB of rolling history
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(_ModuleFilter())
    root.addHandler(file_handler)

    # Per-module files. Everything still lands in the main log too — these are an
    # extra view, not a replacement, so nothing is lost by splitting.
    for module in MODULES:
        h = logging.handlers.RotatingFileHandler(
            LOG_DIR / f"{module}.log",
            maxBytes=10 * 1024 * 1024,   # 10 MB per file
            backupCount=5,               # ~50 MB of per-module history
            encoding="utf-8",
            delay=True,                  # don't create the file until something logs
        )
        h.setFormatter(fmt)
        h.addFilter(_ModuleFilter(only=module))
        root.addHandler(h)

    # Route uvicorn/fastapi records through our root handlers (one format, one file).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    # ponytail: silence watchfiles entirely (not just on the console). Under
    # `uvicorn --reload` it watches the project dir — which contains logs/ — so
    # every log line we write is itself a file change, and it emits "1 change
    # detected" back at us several times a SECOND, forever. It never actually
    # reloads (uvicorn only restarts for *.py), so the whole stream is noise
    # feeding on itself. Dev-only symptom; muting here fixes it everywhere.
    # Cleaner alternative if you'd rather it not scan at all:
    #   uvicorn ... --reload-dir api --reload-dir modules --reload-dir core
    logging.getLogger("watchfiles").setLevel(logging.WARNING)

    _install_faulthandler()
    _install_excepthooks()

    return logging.getLogger("pulse-backend")


def _install_faulthandler() -> None:
    global _fault_log_handle
    # ponytail: setup_logging() is called twice on purpose (once at import, once
    # in startup to out-rank uvicorn's own handlers) — but faulthandler only
    # needs arming once. Without this guard we leaked a second file handle and,
    # back when the periodic dumper existed, ran TWO thread-walkers over the
    # same interpreter. Re-arming buys nothing; enable() is process-global.
    if _fault_log_handle is not None:
        return
    _fault_log_handle = open(LOG_DIR / "faulthandler.log", "a", encoding="utf-8", buffering=1)
    _fault_log_handle.write(f"\n===== faulthandler armed {datetime.now().isoformat()} =====\n")
    faulthandler.enable(file=_fault_log_handle, all_threads=True)
    # ponytail: do NOT arm dump_traceback_later() here. It walks every thread's
    # frames from a native watchdog thread WITHOUT holding the GIL; if a thread
    # frees a frame mid-walk it reads a dead pointer and the process dies with a
    # Windows access violation. Confirmed 2026-07-27: 5 crash dumps (7/7, 7/11,
    # 7/17, 7/18, 7/24) all faulted at the SAME instruction, python312.dll
    # +0x285A8C, reading tiny offsets (0x71/0x5CC4/0x569E) off a null/garbage
    # pointer — the signature of a use-after-free on a PyObject. The AV text was
    # written interleaved inside a periodic dump, i.e. the crash happened during
    # the walk. This diagnostic was causing the very deaths it was added to
    # diagnose. enable() above is safe and still writes a native traceback on a
    # real fault. If hang-diagnosis is ever needed again, dump on demand from an
    # endpoint (faulthandler.dump_traceback) — never on a repeating timer.


def _install_excepthooks() -> None:
    log = logging.getLogger("pulse-backend")

    def _main_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("UNHANDLED EXCEPTION on main thread", exc_info=(exc_type, exc, tb))

    sys.excepthook = _main_hook

    # Catch exceptions that would otherwise silently kill a worker thread
    # (e.g. the refresh pipeline running off BackgroundTasks).
    def _thread_hook(args):
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        name = args.thread.name if args.thread else "?"
        log.critical("UNHANDLED EXCEPTION on thread %s", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = _thread_hook


def install_signal_logging(log: logging.Logger) -> None:
    """Wrap whatever shutdown signal handlers uvicorn installed so we LOG the
    signal before honouring it. Call this from the FastAPI startup hook (after
    uvicorn has installed its own handlers). Only wraps signals whose current
    handler is callable (uvicorn's) — never replaces SIG_DFL/SIG_IGN, so we can't
    accidentally break default behaviour."""
    sigs = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):      # Windows CTRL_BREAK (how a console/Servy stop often arrives)
        sigs.append(signal.SIGBREAK)

    def _handler(signum, frame):
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        log.warning("SIGNAL RECEIVED: %s — initiating graceful shutdown "
                    "(uptime %ss)", name, int(time.time() - _start_time))
        prev = _prev_signal_handlers.get(signum)
        if callable(prev):
            prev(signum, frame)          # chain to uvicorn's handler → graceful stop

    for sig in sigs:
        try:
            current = signal.getsignal(sig)
            if callable(current) and current is not _handler:
                _prev_signal_handlers[sig] = current
                signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Some signals can't be set off the main thread / on this platform.
            pass


def log_startup_banner(log: logging.Logger, port: int | None = None) -> None:
    log.info("=" * 72)
    log.info("IE PULSE BACKEND STARTING")
    log.info("  pid            : %s", os.getpid())
    log.info("  python         : %s", sys.version.split()[0])
    log.info("  cwd            : %s", os.getcwd())
    log.info("  project root   : %s", PROJECT_ROOT)
    log.info("  iedb key found : %s", bool(os.getenv("IEDB_CLIENT_KEY", "").strip()))
    log.info("  log file       : %s", LOG_DIR / MAIN_LOG)
    if port is not None:
        log.info("  port           : %s", port)
    log.info("=" * 72)


def log_shutdown_banner(log: logging.Logger) -> None:
    log.info("-" * 72)
    log.info("IE PULSE BACKEND SHUTTING DOWN — pid %s, uptime %ss",
             os.getpid(), int(time.time() - _start_time))
    log.info("-" * 72)


def start_heartbeat(log: logging.Logger, interval_s: int = 300) -> None:
    """Emit a liveness line every interval_s with uptime + RSS. The last heartbeat
    before the log falls silent is the forensic marker for the moment of death."""
    try:
        import psutil
        proc = psutil.Process()
    except Exception:                    # psutil optional — degrade to uptime only
        proc = None

    def _run():
        while not _hb_stop.wait(interval_s):
            up = int(time.time() - _start_time)
            if proc is not None:
                try:
                    rss = proc.memory_info().rss / (1024 * 1024)
                    log.info("heartbeat — uptime %ss, rss %.0f MB", up, rss)
                    continue
                except Exception:
                    pass
            log.info("heartbeat — uptime %ss", up)

    threading.Thread(target=_run, name="heartbeat", daemon=True).start()


def stop_heartbeat() -> None:
    _hb_stop.set()


@contextmanager
def task_run(log: logging.Logger, mode: str = "", trigger: str = "manual"):
    """Bracket a pipeline run with RUN START / RUN OK / RUN FAILED lines.

    This is what makes `logs/<module>.log` readable as a run history — grep for
    "RUN " and you get every execution, when it ran, how long it took, and
    whether it worked. Failures log the traceback and re-raise, so the process
    still exits non-zero and Task Scheduler records LastTaskResult != 0.
    """
    t0 = time.time()
    log.info("RUN START  mode=%s trigger=%s", mode or "-", trigger)
    try:
        yield
    except BaseException as e:
        log.error("RUN FAILED after %.1fs — %s: %s",
                  time.time() - t0, type(e).__name__, e, exc_info=True)
        raise
    else:
        log.info("RUN OK     %.1fs", time.time() - t0)


if __name__ == "__main__":
    # ponytail: self-check for the two bits of branch logic here — the console
    # filter and the module-tag mapping. Run: python -m core.logging_setup
    def _rec(name, level, msg):
        return logging.LogRecord(name, level, __file__, 1, msg, None, None)

    f = _ConsoleNoiseFilter()
    I, W = logging.INFO, logging.WARNING

    # muted on the console
    assert not f.filter(_rec("uvicorn.access", I, 'GET /api/ole 200'))
    assert not f.filter(_rec("pulse-backend", I, "heartbeat - uptime 60s, rss 140 MB"))

    # never muted
    assert f.filter(_rec("uvicorn.access", W, "slow request"))
    assert f.filter(_rec("pulse-backend", logging.ERROR, "heartbeat thread died"))
    assert f.filter(_rec("pulse-backend", I, "IE PULSE BACKEND STARTING"))
    assert f.filter(_rec("uvicorn.error", I, "Application startup complete."))

    # module tag mapping — underscores become hyphens so tags match the URL paths
    assert _tag_for("modules.cycle_time.client")          == "cycle-time"
    assert _tag_for("modules.ole.pipeline.refresh")       == "ole"
    assert _tag_for("api.routers.ole")                    == "ole"
    assert _tag_for("api.routers.cycle_time")             == "cycle-time"
    assert _tag_for("uvicorn.access")                     == "http"
    assert _tag_for("pulse-backend")                      == "core"
    assert _tag_for("modules")                            == "core"   # too short to index

    # a per-module handler keeps only its own module
    only_ole = _ModuleFilter(only="ole")
    assert only_ole.filter(_rec("modules.ole.pipeline.refresh", I, "x"))
    assert not only_ole.filter(_rec("modules.cycle_time.client", I, "x"))

    # the stamp the formatter depends on is always present
    r = _rec("modules.ppqt.pipeline.refresh", I, "x")
    _ModuleFilter().filter(r)
    assert (r.module_tag, r.short) == ("ppqt", "refresh")

    print("logging self-check OK")
