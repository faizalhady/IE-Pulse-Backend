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
    line every 60s records uptime + RSS. The LAST heartbeat before the log goes
    quiet pinpoints the moment of death and whether memory was climbing
    (OOM-kill) vs a clean signalled stop (which logs a shutdown banner).

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
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"

_FMT = "%(asctime)s  %(levelname)-7s [%(name)s] %(message)s"

# Module-level so they live for the whole process (don't get GC'd).
_fault_log_handle = None
_prev_signal_handlers: dict = {}
_hb_stop = threading.Event()
_start_time = time.time()


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
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "ole-backend.log",
        maxBytes=10 * 1024 * 1024,       # 10 MB per file
        backupCount=10,                  # ~100 MB of rolling history
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Route uvicorn/fastapi records through our root handlers (one format, one file).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    _install_faulthandler()
    _install_excepthooks()

    return logging.getLogger("ole-backend")


def _install_faulthandler() -> None:
    global _fault_log_handle
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
    log = logging.getLogger("ole-backend")

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
    log.info("OLE-BACKEND STARTING")
    log.info("  pid            : %s", os.getpid())
    log.info("  python         : %s", sys.version.split()[0])
    log.info("  cwd            : %s", os.getcwd())
    log.info("  project root   : %s", PROJECT_ROOT)
    log.info("  iedb key found : %s", bool(os.getenv("IEDB_CLIENT_KEY", "").strip()))
    log.info("  log file       : %s", LOG_DIR / "ole-backend.log")
    if port is not None:
        log.info("  port           : %s", port)
    log.info("=" * 72)


def log_shutdown_banner(log: logging.Logger) -> None:
    log.info("-" * 72)
    log.info("OLE-BACKEND SHUTTING DOWN — pid %s, uptime %ss",
             os.getpid(), int(time.time() - _start_time))
    log.info("-" * 72)


def start_heartbeat(log: logging.Logger, interval_s: int = 60) -> None:
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
