"""Self-checks for auth: the developer-lockout guard, and that every write
endpoint in the app carries an identity dependency.

Run: python tests/test_access_guard.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from api.routers.access import _guard_demotion


def _conn(*devs_and_others):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE user_access (ntid TEXT PRIMARY KEY, level TEXT)")
    c.executemany("INSERT INTO user_access VALUES (?,?)", devs_and_others)
    return c


def _denied(conn, ntid, caller, level):
    try:
        _guard_demotion(conn, ntid, caller, level)
        return False
    except HTTPException as e:
        return e.status_code == 403


def no_open_writes():
    """Every POST/PUT/DELETE must resolve the caller. Catches the next router
    that ships a write without one — the whole point of the 5 Aug sweep."""
    from fastapi.routing import APIRoute

    from api.main import app

    open_writes = []
    for r in app.routes:
        if not isinstance(r, APIRoute) or not (r.methods & {"POST", "PUT", "DELETE", "PATCH"}):
            continue
        names = {getattr(d.call, "__name__", "") for d in r.dependant.dependencies}
        # 'dep' is the closure require_level() returns.
        if not names & {"verified_ntid", "dep"}:
            open_writes.append(f"{sorted(r.methods)[0]} {r.path}")
    assert not open_writes, "unauthenticated writes: " + ", ".join(sorted(open_writes))


def demo():
    two = ("a", "developer"), ("b", "developer")

    # self-demotion and self-deletion are refused even with a spare developer
    assert _denied(_conn(*two), "a", "a", "viewer")
    assert _denied(_conn(*two), "A", "a", None)          # ntid case must not matter

    # demoting the OTHER developer is fine while two exist...
    _guard_demotion(_conn(*two), "b", "a", "viewer")
    # ...but not when they are the last one
    assert _denied(_conn(("a", "developer"), ("b", "viewer")), "a", "b", "viewer")

    # non-developers, unknown ntids, and no-op re-saves are never blocked
    _guard_demotion(_conn(("a", "developer"), ("b", "admin")), "b", "a", "viewer")
    _guard_demotion(_conn(("a", "developer")), "nobody", "a", "viewer")
    _guard_demotion(_conn(("a", "developer")), "a", "a", "developer")

    no_open_writes()
    print("ok")


if __name__ == "__main__":
    demo()
