"""
sqllane.py  (cycle_time.chat)
─────────────────────────────
The open-question lane: the model writes ONE SELECT over the two facts views,
inside a cage, and the SQL ships with the answer.

WHY THIS LANE EXISTS
  Nine tools cover the head — the same questions everyone asks. The tail
  ("top 5 workcells by unmapped steps", "average coverage above 1000 units")
  cannot all become tools, and without this lane the router forces them into
  the nearest wrong tool. Text-to-SQL is the right tool for the tail ONLY
  because facts.py pre-solved the semantics: the model never joins, never
  derives a percentage, never guesses a spelling.

THE CAGE, LAYER BY LAYER — each exists because prompts do not enforce anything
  - grammar: the reply is forced to {"sql": "..."} (Ollama format schema)
  - parse gate: one statement, must start SELECT/WITH, no write/DDL keyword
    (checked with string literals stripped, so 'drop' in a name cannot trip it)
  - table gate: every FROM/JOIN target must be one of the two views or a CTE
    defined in the query itself
  - engine gate: fresh in-memory DuckDB holding ONLY the two registered
    frames, with enable_external_access=false — read_parquet/httpfs are dead
    even if a keyword slipped through
  - workcell repair: every workcell literal is rewritten through canon(), so
    "lam research" copied verbatim still hits LAMRESEARCH
  - row/column caps: the query is wrapped in SELECT * FROM (...) LIMIT 50
  - one retry: an execution error goes back to the model once, with the error
  - the SQL is returned in the payload and rendered under the answer — a wrong
    query is checkable, not invisible

WHAT A FAILURE LOOKS LIKE
  A refusal with the reason, never a guess: {"error": "sql_failed", ...}. The
  agent turns that into "I could not build a query for that" — worse answers
  than that are exactly what this module was built to avoid.
"""

from __future__ import annotations

import json
import logging
import re

import duckdb

from modules.cycle_time.chat import facts, ollama
from modules.cycle_time.model_universe import canon

log = logging.getLogger(__name__)

_SQL_FORM = {"type": "object", "properties": {"sql": {"type": "string"}},
             "required": ["sql"]}

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|pragma|"
    r"install|load|export|import|call|set|reset|vacuum|checkpoint|begin|"
    r"commit|rollback|grant|truncate|merge)\b", re.I)

_TABLES = {"llm_model_facts", "llm_workcell_facts"}

#: Few-shots as real message pairs — they teach the two habits the DDL alone
#: does not: filter on workcell_key with the name normalised, and aggregate
#: from the workcell view when the question is per-workcell.
_SHOTS = [
    ("top 5 workcells by unmapped steps",
     "SELECT workcell, unmapped_steps FROM llm_workcell_facts "
     "ORDER BY unmapped_steps DESC LIMIT 5"),
    ("average coverage of lam research models in demand",
     "SELECT ROUND(AVG(coverage_pct), 1) AS avg_coverage_pct "
     "FROM llm_model_facts WHERE workcell_key = 'LAMRESEARCH' AND has_demand"),
    ("how many models per plant",
     "SELECT plant, COUNT(*) AS models FROM llm_model_facts "
     "GROUP BY plant ORDER BY models DESC"),
]


def _prompt() -> str:
    return (
        "Write ONE DuckDB SELECT statement that answers the user's question, "
        "using ONLY these tables:\n\n" + facts.ddl() + "\n\n"
        "Rules:\n"
        "- SELECT only, one statement.\n"
        "- Filter workcells on workcell_key: the name in UPPERCASE letters and "
        "digits only ('lam research' -> 'LAMRESEARCH').\n"
        "- status values are exactly the six listed; 'planned' or 'in demand' "
        "means WHERE has_demand.\n"
        "- Name result columns clearly. ROUND percentages to 1 decimal.\n"
        "- At most LIMIT 50."
    )


def _strip_strings(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def validate(sql: str) -> str | None:
    """The reason this SQL is refused, or None if it may run."""
    s = sql.strip().rstrip(";").strip()
    if not s:
        return "empty statement"
    if ";" in s:
        return "one statement only"
    if not re.match(r"^(select|with)\b", s, re.I):
        return "must start with SELECT"
    bare = _strip_strings(s)
    m = _FORBIDDEN.search(bare)
    if m:
        return f"forbidden keyword: {m.group(0)}"
    ctes = {c.lower() for c in re.findall(r"\b(\w+)\s+as\s*\(", bare, re.I)}
    for t in re.findall(r"\b(?:from|join)\s+([A-Za-z_][\w.]*)", bare, re.I):
        if t.lower() not in _TABLES and t.lower() not in ctes:
            return f"table not allowed: {t}"
    return None


def _fix_workcells(sql: str) -> str:
    """Rewrite every workcell literal through canon(), and retarget equality on
    the display column to the key column — deterministic repair for the one
    thing few-shots cannot guarantee."""
    sql = re.sub(r"\bworkcell\s*=\s*'([^']*)'",
                 lambda m: f"workcell_key = '{canon(m.group(1))}'", sql, flags=re.I)
    sql = re.sub(r"\b(workcell_key\s*=\s*)'([^']*)'",
                 lambda m: f"{m.group(1)}'{canon(m.group(2))}'", sql, flags=re.I)
    return sql


def _execute(sql: str) -> dict:
    m, w = facts.frames()
    con = duckdb.connect()                       # in-memory: holds ONLY the views
    try:
        con.execute("SET enable_external_access=false")
        con.register("llm_model_facts", m)
        con.register("llm_workcell_facts", w)
        df = con.execute(f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS q LIMIT 50").df()
    finally:
        con.close()
    df = df.iloc[:, :12]
    rows = json.loads(df.to_json(orient="records", date_format="iso"))
    return {"columns": list(df.columns), "rows": rows, "row_count": len(rows)}


def run(question: str) -> dict:
    """-> {_src, sql, columns, rows, row_count} or {error, detail, sql?}."""
    messages = [{"role": "system", "content": _prompt()}]
    for q, sql in _SHOTS:
        messages.append({"role": "user", "content": q})
        messages.append({"role": "assistant", "content": json.dumps({"sql": sql})})
    messages.append({"role": "user", "content": question})

    last_err, sql = "", ""
    for attempt in range(2):                     # one shot + one repair
        try:
            msg = ollama.chat(messages, format=_SQL_FORM)
            sql = str(json.loads(msg.get("content") or "{}").get("sql") or "").strip()
        except (ollama.OllamaError, ValueError) as e:
            return {"error": "sql_failed", "detail": f"model: {e}"}
        bad = validate(sql)
        if bad:
            last_err = bad
        else:
            sql = _fix_workcells(sql)
            try:
                out = _execute(sql)
                log.info("chat sql ok (%d rows): %s", out["row_count"], sql[:200])
                return {"_src": "read-only SQL over the demand mart (llm facts views)",
                        "sql": sql, **out}
            except Exception as e:               # noqa: BLE001 — engine refused it
                last_err = str(e)[:300]
        log.info("chat sql attempt %d refused (%s): %s", attempt + 1, last_err, sql[:200])
        messages.append({"role": "assistant", "content": json.dumps({"sql": sql})})
        messages.append({"role": "user", "content":
                         f"That query failed: {last_err}. Write a corrected SELECT."})
    return {"error": "sql_failed", "detail": last_err, "sql": sql}


def render(out: dict) -> str:
    """Deterministic text for a result table. The model never phrases more than
    one number — a sentence that misquotes a table reads exactly like one that
    did not."""
    rows, cols = out["rows"], out["columns"]
    if not rows:
        return "The query ran but matched nothing."
    if len(rows) == 1 and len(cols) == 1:
        return f"{cols[0].replace('_', ' ')}: {rows[0][cols[0]]}"
    show = rows[:10]
    head = " | ".join(cols)
    line = " | ".join("---" for _ in cols)
    body = "\n".join(" | ".join(str(r.get(c, "")) for c in cols) for r in show)
    extra = f"\n… {len(rows) - len(show)} more rows" if len(rows) > len(show) else ""
    return f"{head}\n{line}\n{body}{extra}"


if __name__ == "__main__":
    # The cage is the part that must hold without a model.
    assert validate("SELECT * FROM llm_model_facts") is None
    assert validate("WITH t AS (SELECT workcell FROM llm_workcell_facts) SELECT * FROM t") is None
    assert validate("DROP TABLE llm_model_facts")
    assert validate("SELECT * FROM other_table")
    assert validate("SELECT 1; SELECT 2")
    assert validate("SELECT * FROM llm_model_facts WHERE reason = 'drop table'") is None
    assert validate("INSTALL httpfs")
    assert _fix_workcells("workcell = 'lam research'") == "workcell_key = 'LAMRESEARCH'"
    assert _fix_workcells("workcell_key = 'key sight'") == "workcell_key = 'KEYSIGHT'"
    r = _execute("SELECT workcell_key, models FROM llm_workcell_facts ORDER BY models DESC")
    assert r["row_count"] <= 50 and "models" in r["columns"]
    try:
        _execute("SELECT * FROM read_parquet('data/mart/cycle_time/raw.parquet')")
        raise SystemExit("FAIL: external access must be blocked")
    except Exception:
        pass
    print("sqllane self-check OK —", r["row_count"], "workcells via SQL")
