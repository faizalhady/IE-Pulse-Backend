"""
modules/universe/tools.py
─────────────────────────
The three tools a model gets: describe · query · define. Plain functions here;
mcp_server.py exposes them over MCP, eval/run.py calls them directly.

  describe(view=None)  the semantic views with every column's comment — read this first
  query(sql)           ONE caged SELECT over the v_* views, capped at MAX_ROWS
  define(term)         the rules, traps and vocabulary behind a word (the jabil-universe skill)

THE CAGE (carried from chat v1 — the part that was right)
  one statement · SELECT/WITH only · tables limited to the allow-listed views ·
  no file/remote functions · enable_external_access = false · LIMIT capped ·
  30 s interrupt · the SQL is returned with every result.
  v_employee is NOT in the allow-list for the trial: payroll names stay inside.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import duckdb

from modules.universe import views as V

MAX_ROWS = 200
TIMEOUT_S = 30
HIDDEN_VIEWS = {"v_employee"}
ALLOWED_VIEWS = tuple(v for v in V.VIEWS if v not in HIDDEN_VIEWS)
SKILL_DIR = Path.home() / ".claude" / "skills" / "jabil-universe"

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|pragma|install|load|export|"
    r"import|call|set|reset|vacuum|checkpoint|begin|commit|rollback|grant|truncate|merge|"
    r"read_parquet|read_csv|read_csv_auto|read_json|glob|getenv)\b", re.I)

_con: duckdb.DuckDBPyConnection | None = None
_lock = threading.Lock()


def _connection() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        _con = V.connect()
        # The views read the universe parquet lazily, so a blanket external-access
        # ban would block them too. Allow exactly the mart folder, then close the door:
        # nothing outside it — no other files, no network — is reachable from SQL.
        from modules.universe.config import UNIVERSE_MART_DIR
        _con.execute(f"set allowed_directories = ['{UNIVERSE_MART_DIR.as_posix()}/']")
        _con.execute("set enable_external_access = false")
    return _con


def reset() -> None:
    global _con
    _con = None


# ─── describe ────────────────────────────────────────────────────────────────

def describe(view: str | None = None) -> list[dict]:
    """The views and their columns, each with its comment — the meaning a model
    cannot get from names alone. One view when named, all of them otherwise."""
    if view and view not in ALLOWED_VIEWS:
        return []
    con = _connection()
    out = []
    for v in ([view] if view else ALLOWED_VIEWS):
        cols = con.execute(
            "select column_name, data_type, comment from duckdb_columns() where table_name = ? order by column_index",
            [v]).fetchall()
        out.append({"view": v, "columns": [{"name": c, "type": t, "comment": cm or ""} for c, t, cm in cols]})
    return out


# ─── query ───────────────────────────────────────────────────────────────────

def _strip_strings(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def validate(sql: str) -> str | None:
    """Why this SQL is refused, or None."""
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return "empty statement"
    if ";" in s:
        return "one statement only"
    if not re.match(r"^(select|with)\b", s, re.I):
        return "must start with SELECT (or WITH)"
    bare = _strip_strings(s)
    m = _FORBIDDEN.search(bare)
    if m:
        return f"not allowed: {m.group(0)}"
    ctes = {c.lower() for c in re.findall(r"\b(\w+)\s+as\s*\(", bare, re.I)}
    for t in re.findall(r"\b(?:from|join)\s+([A-Za-z_][\w.]*)", bare, re.I):
        if t.lower() not in ALLOWED_VIEWS and t.lower() not in ctes:
            return f"table not allowed: {t} — query the views only: {', '.join(ALLOWED_VIEWS)}"
    return None


def query(sql: str) -> dict:
    """Run one caged SELECT over the views. -> {columns, rows, row_count, sql, truncated} or {error, detail}."""
    bad = validate(sql)
    if bad:
        return {"error": "sql_rejected", "detail": bad, "sql": sql}
    wrapped = f"select * from ({sql.strip().rstrip(';')}) as q limit {MAX_ROWS}"
    con = _connection()
    timer = threading.Timer(TIMEOUT_S, con.interrupt)
    with _lock:
        timer.start()
        try:
            rel = con.execute(wrapped)
            cols = [d[0] for d in rel.description]
            rows = rel.fetchall()
        except Exception as e:                     # noqa: BLE001 — refused by the engine, or interrupted
            return {"error": "sql_failed", "detail": str(e)[:400], "sql": wrapped}
        finally:
            timer.cancel()
    return {"columns": cols,
            "rows": [dict(zip(cols, (str(v) if not isinstance(v, (int, float, bool, type(None))) else v for v in r))) for r in rows],
            "row_count": len(rows), "truncated": len(rows) >= MAX_ROWS, "sql": wrapped}


# ─── define ──────────────────────────────────────────────────────────────────

def _passages() -> list[tuple[str, str]]:
    """(source, passage) from the skill — table rows and paragraphs, small enough to quote."""
    out = []
    for f in [SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md"))]:
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        for block in re.split(r"\n\s*\n", text):
            block = block.strip()
            if not block or block.startswith("---"):
                continue
            if block.startswith("|"):
                for line in block.splitlines():
                    if line.startswith("|") and not re.match(r"^\|\s*-", line) and "| Word |" not in line and "| # |" not in line:
                        out.append((f.name, line))
            else:
                out.append((f.name, block))
    return out


def define(term: str, limit: int = 8) -> list[dict]:
    """Passages from the rules, traps and vocabulary that mention every word of the term."""
    words = [w for w in re.findall(r"[a-z0-9_]+", (term or "").lower()) if len(w) > 1]
    if not words:
        return []
    hits = []
    for src, text in _passages():
        low = text.lower()
        if all(w in low for w in words):
            score = sum(low.count(w) for w in words) + (5 if "**" in text else 0)
            hits.append((score, src, text))
    hits.sort(key=lambda h: -h[0])
    return [{"source": s, "text": t[:1200]} for _, s, t in hits[:limit]]


if __name__ == "__main__":
    print([v["view"] for v in describe()])
    print(query("select workcell, status from v_workcell order by 1 limit 3"))
    print(define("terminal step")[:2])
