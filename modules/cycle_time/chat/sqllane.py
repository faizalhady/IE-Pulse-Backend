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

from modules.cycle_time.chat import facts, llm
from modules.cycle_time.model_universe import canon

log = logging.getLogger(__name__)

_SQL_FORM = {"type": "object", "properties": {"sql": {"type": "string"}},
             "required": ["sql"]}

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|pragma|"
    r"install|load|export|import|call|set|reset|vacuum|checkpoint|begin|"
    r"commit|rollback|grant|truncate|merge)\b", re.I)

#: Allowlist derives from the facts registry — a new view is queryable the
#: moment it exists, and a typo'd table name still gets refused.
_TABLES = set(facts.VIEWS)

#: A superlative question answered by an UNORDERED query is the worst failure
#: this lane can produce: 50 arbitrary rows, and the lead-in sentence then
#: "answers" the superlative from whichever 8 it was shown. Measured, not
#: hypothetical — "longest cycle time" came back as a bare GROUP BY and the
#: sentence invented a winner. Shape-checked here, deterministically.
_SUPERLATIVE_RE = re.compile(
    r"\b(longest|shortest|slowest|fastest|highest|lowest|most|least|"
    r"max(?:imum)?|min(?:imum)?|biggest|smallest|top\s*\d*|worst|best)\b", re.I)

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
    ("which process from which model has the longest cycle time",
     "SELECT workcell, assembly, process, ct_seconds FROM llm_process_facts "
     "ORDER BY ct_seconds DESC LIMIT 1"),
]


def _prompt() -> str:
    from datetime import date
    today = date.today()
    return (
        f"TODAY is {today.isoformat()} ({today.strftime('%A')}).\n"
        "Write ONE DuckDB SELECT statement that answers the user's question, "
        "using ONLY these tables:\n\n" + facts.ddl() + "\n\n"
        "Rules:\n"
        "- Date columns are ISO TEXT — compare with CAST(col AS DATE); "
        "CURRENT_DATE works in DuckDB.\n"
        "- SELECT only, one statement.\n"
        "- Filter workcells on workcell_key: the name in UPPERCASE letters and "
        "digits only ('lam research' -> 'LAMRESEARCH').\n"
        "- status values are exactly the six listed; 'planned' or 'in demand' "
        "means WHERE has_demand.\n"
        "- Name result columns clearly. ROUND percentages to 1 decimal.\n"
        "- When selecting from llm_model_facts, ALWAYS select workcell next to "
        "assembly — a model is (workcell, assembly) together.\n"
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


def _ensure_workcell(sql: str) -> str:
    """A model is (workcell, assembly) TOGETHER — an assembly column alone is
    half an identity, unclickable in the UI and ambiguous across workcells.
    When a model-facts query projects assembly without workcell, workcell is
    added to the SELECT (and to a GROUP BY that groups on assembly). Anything
    this cannot rewrite safely — CTEs, grouping not on assembly — is left
    exactly as written."""
    if not re.search(r"\bllm_(model_facts|process_facts|route_steps|demand_weekly|builds)\b", sql, re.I):
        return sql
    if re.search(r"\bworkcell\b", sql, re.I):
        return sql
    m = re.match(r"^\s*select\s+(distinct\s+)?(.+?)\s+from\b", sql, re.I | re.S)
    if not m or not re.search(r"\bassembly\b", m.group(2), re.I):
        return sql
    gb = re.search(r"\bgroup\s+by\s+(.+?)(?=\border\s+by\b|\blimit\b|$)", sql, re.I | re.S)
    if gb and not re.search(r"\bassembly\b", gb.group(1), re.I):
        return sql                               # grouped on something else — do not touch
    out = sql[:m.start(2)] + "workcell, " + sql[m.start(2):]
    if gb:
        g2 = re.search(r"\bgroup\s+by\s+", out, re.I)
        out = out[:g2.end()] + "workcell, " + out[g2.end():]
    return out


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
    con = duckdb.connect()                       # in-memory: holds ONLY the views
    try:
        con.execute("SET enable_external_access=false")
        for name, frame in facts.frames().items():
            con.register(name, frame)
        df = con.execute(f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS q LIMIT 50").df()
    finally:
        con.close()
    df = df.iloc[:, :12]
    rows = json.loads(df.to_json(orient="records", date_format="iso"))
    return {"columns": list(df.columns), "rows": rows, "row_count": len(rows)}


def execute_checked(sql: str, question: str = "") -> dict:
    """The cage without the model: validate, rewrite, execute one SELECT the
    caller already has. The agentic loop uses this — the big model writes its
    own SQL as a tool argument, and every layer of the cage still applies."""
    bad = validate(sql)
    if not bad and question and _SUPERLATIVE_RE.search(question) \
            and not re.search(r"\border\s+by\b", sql, re.I):
        bad = ("the question asks for a superlative but the query has no "
               "ORDER BY - add ORDER BY <measure> DESC/ASC with a small LIMIT")
    if bad:
        return {"error": "sql_rejected", "detail": bad, "sql": sql}
    fixed = _ensure_workcell(_fix_workcells(sql))
    try:
        out = _execute(fixed)
    except Exception as e:                       # noqa: BLE001
        return {"error": "sql_failed", "detail": str(e)[:300], "sql": fixed}
    return {"_src": "read-only SQL over the demand mart (llm facts views)",
            "sql": fixed, **out}


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
            msg = llm.chat(messages, format=_SQL_FORM)
            sql = str(json.loads(msg.get("content") or "{}").get("sql") or "").strip()
        except (llm.LLMError, ValueError) as e:
            return {"error": "sql_failed", "detail": f"model: {e}"}
        bad = validate(sql)
        if not bad and _SUPERLATIVE_RE.search(question) \
                and not re.search(r"\border\s+by\b", sql, re.I):
            bad = ("the question asks for a superlative (longest/most/top) but "
                   "the query has no ORDER BY — add ORDER BY <the measure> "
                   "DESC or ASC with a small LIMIT")
        if bad:
            last_err = bad
        else:
            sql = _ensure_workcell(_fix_workcells(sql))
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
    # assembly never travels without its workcell
    assert _ensure_workcell("SELECT assembly, units FROM llm_model_facts ORDER BY units DESC LIMIT 3") \
        == "SELECT workcell, assembly, units FROM llm_model_facts ORDER BY units DESC LIMIT 3"
    assert _ensure_workcell("SELECT assembly, COUNT(*) c FROM llm_model_facts GROUP BY assembly") \
        == "SELECT workcell, assembly, COUNT(*) c FROM llm_model_facts GROUP BY workcell, assembly"
    s = "SELECT workcell, assembly FROM llm_model_facts"
    assert _ensure_workcell(s) == s                       # already there — untouched
    s = "SELECT plant, COUNT(*) FROM llm_model_facts GROUP BY plant"
    assert _ensure_workcell(s) == s                       # no assembly — untouched
    s = "SELECT assembly FROM llm_workcell_facts"
    assert _ensure_workcell(s) == s                       # wrong table — untouched
    # the superlative guard: an ORDERED query passes, an unordered one is refused
    # (line 190 once held literal backspace bytes instead of \b and refused both)
    q = "two longest cycle time keysight models"
    assert "error" not in execute_checked(
        "SELECT assembly, ct_seconds FROM llm_process_facts ORDER BY ct_seconds DESC LIMIT 2", q)
    assert execute_checked("SELECT assembly, ct_seconds FROM llm_process_facts LIMIT 2", q)["error"] == "sql_rejected"
    r = _execute("SELECT workcell_key, models FROM llm_workcell_facts ORDER BY models DESC")
    assert r["row_count"] <= 50 and "models" in r["columns"]
    try:
        _execute("SELECT * FROM read_parquet('data/mart/cycle_time/raw.parquet')")
        raise SystemExit("FAIL: external access must be blocked")
    except Exception:
        pass
    print("sqllane self-check OK —", r["row_count"], "workcells via SQL")
