"""
core/mart_cache.py
──────────────────
Cache expensive mart-derived results without ever serving stale data.

The problem
───────────
Endpoints re-read parquet and re-run DuckDB on EVERY request. Nothing is
cached, so warm requests cost exactly what cold ones do. Measured on the
server, 2026-08-03:

    /api/cycle-time/aliases                cold 12.1s   warm 11.5s   136 KB
    /api/cycle-time/assembly-list          cold  5.1s   warm  5.2s     4 MB

`/aliases` pulls 4 columns x 4.4M rows into pandas and groups them in Python,
for an answer that only changes when the pipeline rewrites the mart — i.e.
once or twice a day.

The approach
────────────
Key the cache on the mart file's modification time. When the pipeline rewrites
a parquet, its mtime changes, the key changes, and the next call recomputes.

That means there is no TTL to guess at and no invalidation to call. It is
impossible to serve data older than the file on disk, which is the failure mode
a time-based cache always eventually hits.

Usage
─────
    from functools import lru_cache
    from core.mart_cache import mart_key

    @lru_cache(maxsize=32)
    def _compute(customer, _key):      # _key is unused - it only varies the key
        ...expensive work...

    @router.get("/thing")
    def thing(customer=None):
        return _compute(customer, mart_key(CT_MART["raw"]))

Note the cached object is shared between callers. Treat results as read-only —
serialising them (what FastAPI does) is fine; mutating one in place would
poison the cache for everyone.

ponytail: stdlib only. Redis was considered and rejected — one uvicorn process
on one box means a shared cache has nobody to share with, and it would add a
network hop, a serialisation round-trip, and an external service that a process
with a silent-stop history can fail against.
"""

from pathlib import Path


def mart_key(*paths: Path) -> tuple:
    """A cache key that changes whenever any of `paths` is rewritten.

    Uses `st_mtime_ns` (nanoseconds) rather than `st_mtime` (float seconds):
    a pipeline that rewrites a mart twice inside the same second would
    otherwise reuse the stale entry.

    A missing file keys as 0, so an endpoint cached before its mart existed
    recomputes once the pipeline creates it.
    """
    return tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in paths)


if __name__ == "__main__":
    # ponytail: self-check for the one thing that must not break - a rewritten
    # mart MUST produce a different key. Run: python -m core.mart_cache
    import tempfile
    import time
    from functools import lru_cache

    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "mart.parquet"
        missing = Path(d) / "not-built-yet.parquet"

        assert mart_key(missing) == (0,), "missing file must key as 0"

        f.write_text("v1")
        k1 = mart_key(f)
        assert mart_key(f) == k1, "unchanged file must give a stable key"

        calls = []

        @lru_cache(maxsize=8)
        def _compute(arg, _key):
            calls.append(arg)
            return f"{arg}:{f.read_text()}"

        assert _compute("x", mart_key(f)) == "x:v1"
        assert _compute("x", mart_key(f)) == "x:v1"
        assert len(calls) == 1, "second call must be served from cache"

        # different argument, same mart -> recompute, cache is per-argument
        _compute("y", mart_key(f))
        assert len(calls) == 2

        time.sleep(0.01)
        f.write_text("v2")                       # pipeline rewrites the mart
        assert mart_key(f) != k1, "rewritten file must change the key"
        assert _compute("x", mart_key(f)) == "x:v2", "must serve fresh data"
        assert len(calls) == 3, "rewrite must force a recompute"

        # a two-mart key changes if EITHER file changes
        g = Path(d) / "other.parquet"
        g.write_text("a")
        both = mart_key(f, g)
        time.sleep(0.01)
        g.write_text("b")
        assert mart_key(f, g) != both

    print("mart_cache self-check OK")
