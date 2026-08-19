"""registry — the process registry, served to the UI.

WHAT THIS IS FOR
  MES and IEDB name the same process differently, and neither name is
  controlled. The registry lines them up per workcell. Most of it is derived;
  a residue cannot be:

      105 MES step names across 14 workcells that nothing maps.

  Matching them by name is 38% right, by neighbouring scan 27%, by bay 55%.
  Too wrong to apply — a wrong mapping is worse than a blank. The engineer who
  works the line knows instantly. This module hands them the question with its
  evidence and records the answer.

THE LOOP THIS CLOSES
    registry CSVs -> API -> page -> engineer answers -> SQLite
         ^                                                 |
         +------ generators re-run <-- process_decision.csv +

  `_registry_keys.load_bridge()` in the registry repo already reads
  `process_decision.csv` and lets it override the workbook, so answers survive
  the Excel seed being regenerated.

WHY CSV AND NOT PARQUET
  These are the registry's own outputs, copied in. They are small (520 KB),
  human-readable, and the registry repo is the source of truth — the backend
  keeps a copy rather than reaching across repos at request time.
"""

from __future__ import annotations

import csv
import logging
import re
from functools import lru_cache

import duckdb
import pandas as pd

from core.database import get_conn
from modules.cycle_time.config import CT_MART

log = logging.getLogger(__name__)

REG_DIR = CT_MART["raw"].parent / "registry"
PROCESSES = REG_DIR / "workcell_process.csv"
QUESTIONS = REG_DIR / "unmapped_suggest.csv"

_wcnorm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _mtime() -> float:
    """Cache key. The marts are rebuilt by a generator, not by this process, so
    the cache must key on the file itself — the same trick the rest of the
    module uses (`mart_key`)."""
    return max((p.stat().st_mtime for p in (PROCESSES, QUESTIONS) if p.exists()),
               default=0.0)


@lru_cache(maxsize=4)
def _load(_key: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not PROCESSES.exists():
        raise FileNotFoundError(
            f"registry not found at {REG_DIR}. Copy workcell_process.csv and "
            f"unmapped_suggest.csv from Projects/docs/registry.")
    proc = pd.read_csv(PROCESSES).fillna("")
    ques = pd.read_csv(QUESTIONS).fillna("") if QUESTIONS.exists() else pd.DataFrame()
    return proc, ques


def _frames():
    return _load(_mtime())


def workcells() -> list[dict]:
    """Every workcell with a registry, and how much of it is still unanswered."""
    proc, ques = _frames()
    done = {(_wcnorm(w), s) for w, s in _decisions_index()}
    out = []
    for name, g in proc.groupby("workcell"):
        q = ques[ques["workcell"] == name] if len(ques) else pd.DataFrame()
        left = sum(1 for s in q.get("name_raw", []) if (_wcnorm(name), s) not in done)
        out.append({
            "workcell": name,
            "processes": int(g["process_key"].nunique()),
            "agreed": int((g["source"] == "both").sum()),
            "iedb_only": int((g["source"] == "iedb_only").sum()),
            "gap": int((g["source"] == "mes_only").sum()),
            "questions_total": int(len(q)),
            "questions_left": left,
        })
    return sorted(out, key=lambda r: (-r["questions_left"], r["workcell"]))


def processes(workcell: str) -> list[dict]:
    """The browse view: every process this workcell runs, both systems' names."""
    proc, _ = _frames()
    g = proc[proc["workcell"].map(_wcnorm) == _wcnorm(workcell)]
    cols = ["process_key", "process_family", "process_name", "workcenter", "source",
            "iedb_aliases", "mes_steps", "iedb_models", "iedb_rows", "mes_models",
            "mes_scans", "review"]
    g = g[[c for c in cols if c in g.columns]]
    # worst first: the gap is what someone came here to see
    order = {"mes_only": 0, "iedb_only": 1, "both": 2, "mes_non_iedb": 3}
    g = g.assign(_o=g["source"].map(order).fillna(9)).sort_values(
        ["_o", "mes_scans"], ascending=[True, False]).drop(columns="_o")
    return g.to_dict("records")


def _decisions_index() -> list[tuple[str, str]]:
    with get_conn() as c:
        return [(r["workcell"], r["mes_step"]) for r in
                c.execute("SELECT workcell, mes_step FROM process_decision")]


def questions(workcell: str, include_answered: bool = False) -> list[dict]:
    """The queue. Sorted by scans DESCENDING, always.

    An engineer who answers the top 20 and stops has still covered most of the
    volume. Alphabetical order would spend their attention on a step scanned
    once."""
    _, ques = _frames()
    if not len(ques):
        return []
    g = ques[ques["workcell"].map(_wcnorm) == _wcnorm(workcell)].copy()

    with get_conn() as c:
        answered = {r["mes_step"]: dict(r) for r in c.execute(
            "SELECT * FROM process_decision WHERE workcell = ?", (workcell,))}

    # .astype(bool) is load-bearing. On a workcell with NO questions, `.map()`
    # returns an empty OBJECT-dtype Series, `~` on it does not give a boolean
    # mask, and `g[mask]` is then read as COLUMN selection — which returns a
    # frame with no columns at all, and the next line dies on KeyError 'scans'.
    # Every workcell whose queue is empty 500s without this.
    g["answered"] = g["name_raw"].map(lambda s: s in answered).astype(bool)
    if not include_answered:
        g = g[~g["answered"]]
    g["scans"] = pd.to_numeric(g["scans"], errors="coerce").fillna(0)
    g = g.sort_values("scans", ascending=False)

    out = []
    for _, r in g.iterrows():
        prior = answered.get(r["name_raw"], {})
        out.append({
            "workcell": r["workcell"],
            "mes_step": r["name_raw"],
            "models": int(pd.to_numeric(r.get("models"), errors="coerce") or 0),
            "scans": int(r["scans"]),
            "bay": r.get("bay", ""),
            "scanned_before": r.get("scanned_before", ""),
            "scanned_after": r.get("scanned_after", ""),
            "candidates": [c for c in str(r.get("name_candidates_on_that_line", "")).split() if c],
            "suggestion": r.get("suggestion", ""),
            "confidence": r.get("confidence", ""),
            "answered": bool(r["answered"]),
            "prior_answer": prior.get("answer"),
            "prior_alias": prior.get("iedb_alias"),
            "prior_by": prior.get("decided_by"),
        })
    return out


# ── the process list ──────────────────────────────────────────────────────
#
# ONE function behind both process views, because they differ only in which
# `system` rows they read:
#
#   scanned     5,344 (workcell, step) couples MES actually recorded a scan on.
#               This is the work list — answering these 100% is the objective.
#   configured  72,692 couples MES has on a route. 67,394 of them have never
#               been scanned. Listing them is fine; treating them as the queue
#               is not, which is why `scanned` is the default.
#
# The mapping grain is (workcell, step) and nothing finer. Within a workcell no
# step name resolves to two different IEDB aliases — 0 of 9,734 — so a model
# column would multiply the queue by every assembly and buy nothing.


def _stepnorm(s) -> str:
    """Whitespace-collapsed upper — the workbook's key, not a match key for
    IEDB names. Trailing and double spaces in the raw name are EVIDENCE and are
    never destroyed; this is only used to look the raw name up."""
    return re.sub(r"\s+", " ", str(s or "").strip().upper())


RAW = REG_DIR / "workcell_process_raw.csv"


@lru_cache(maxsize=2)
def _load_raw(_key: float) -> pd.DataFrame:
    """11.9 MB of CSV. `steps()` used to re-read it on every keystroke of the
    filter box; the global list is 72k rows and could not afford that at all."""
    df = pd.read_csv(RAW).fillna("")
    df["_wc"] = df["workcell"].map(_wcnorm)
    df["_step"] = df["name_raw"].map(_stepnorm)
    for c in ("rows", "models", "scans"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
    return df


def _raw_frame() -> pd.DataFrame:
    if not RAW.exists():
        return pd.DataFrame()
    return _load_raw(RAW.stat().st_mtime)


@lru_cache(maxsize=2)
def _book(_key: float) -> dict:
    """The hand-typed workbook: (workcell, step) -> IEDB alias.

    A key present with an EMPTY alias is not missing data — it is the workbook
    saying 'this step is not an IEDB process'. Exactly the 1,961 `non_iedb`
    steps in the raw file, so the two agree row for row."""
    try:
        pm = pd.read_parquet(CT_MART["mes_process_map"])
        return {(_wcnorm(c), _stepnorm(s)): str(a or "")
                for c, s, a in zip(pm["customer"], pm["step_instance"], pm["iedb_alias"])}
    except Exception:                                   # never block the view
        return {}


def _book_map() -> dict:
    p = CT_MART["mes_process_map"]
    return _book(p.stat().st_mtime if p.exists() else 0.0)


def _decisions_all() -> dict:
    """Every engineer answer, keyed (workcell, step) with the step BYTE-EXACT.
    The step name is what was stored; normalising it here would silently match
    a decision to the wrong one of two names that differ only in spacing."""
    with get_conn() as c:
        return {(_wcnorm(r["workcell"]), str(r["mes_step"])): dict(r)
                for r in c.execute("SELECT * FROM process_decision")}


# `scans` is 0 on all 82,010 rows of workcell_process_raw.csv — the column was
# never populated. `rows` (MES scan records) is the only real volume signal in
# this file, so it is the default sort. Sorting by scans was sorting by nothing.
#: sort key -> column in the annotated frame
_SORTS = {"step": "name_raw", "workcell": "workcell", "status": "status",
          "source": "source", "maps_to": "iedb_alias", "models": "models",
          "rows": "rows", "scans": "scans"}


def process_list(scope: str = "scanned", workcell: str = "", q: str = "",
                 status: str = "", sort: str = "rows", direction: str = "desc",
                 page: int = 1, page_size: int = 200) -> dict:
    """The process list, one row per (workcell, MES step).

      -> {rows: [...], total, page, page_size, counts: {mapped, non_iedb, unmapped}}

    `status` is the answer, `source` is who gave it. They are separate columns
    because "mapped" and "mapped BY A PLANT-WIDE GUESS" are not the same fact,
    and folding them into one badge is what let bad auto-mappings look settled.

        decision  an engineer answered it here   (overrides everything)
        workbook  the hand-typed Excel sheet
        auto      the raw file's own bridge answer
        none      nothing maps it

    `counts` is over the FILTERED set before paging, so the chips can show how
    much work each one holds without a second round-trip.
    """
    df = _raw_frame()
    if not len(df):
        return {"rows": [], "total": 0, "page": 1, "page_size": page_size,
                "counts": {"mapped": 0, "non_iedb": 0, "unmapped": 0}}

    system = "mes_configured" if scope == "configured" else "mes_step"
    g = df[df["system"] == system]
    if workcell:
        g = g[g["_wc"] == _wcnorm(workcell)]
    g = g.copy()

    dec, book = _decisions_all(), _book_map()

    # Resolution order, highest authority first. Listed once here rather than
    # per row, because the order IS the policy.
    st, al, src, by, on = [], [], [], [], []
    for wc, raw_name, step, ans in zip(g["_wc"], g["name_raw"], g["_step"], g["answer"]):
        d = dec.get((wc, str(raw_name)))
        if d:
            st.append(d["answer"]); al.append(d["iedb_alias"] or "")
            src.append("decision"); by.append(d["decided_by"] or ""); on.append(d["decided_on"] or "")
            continue
        by.append(""); on.append("")
        if (wc, step) in book:
            a = book[(wc, step)]
            st.append("mapped" if a else "non_iedb"); al.append(a); src.append("workbook")
        elif ans in ("mapped", "non_iedb", "unmapped"):
            st.append(ans); al.append(""); src.append("auto")
        else:
            st.append("unmapped"); al.append(""); src.append("none")
    g["status"], g["iedb_alias"] = st, al
    g["source"], g["decided_by"], g["decided_on"] = src, by, on

    if q:
        n = q.strip().lower()
        g = g[g["name_raw"].str.lower().str.contains(n, regex=False)
              | g["iedb_alias"].str.lower().str.contains(n, regex=False)
              | g["workcell"].str.lower().str.contains(n, regex=False)]
    counts = {k: int((g["status"] == k).sum()) for k in ("mapped", "non_iedb", "unmapped")}
    if status:
        g = g[g["status"].isin([s for s in status.split(",") if s])]

    total = len(g)
    col = _SORTS.get(sort, "rows")
    g = g.sort_values(col, ascending=(direction == "asc"), kind="stable")

    page = max(1, page)
    lo = (page - 1) * page_size
    out = g.iloc[lo:lo + page_size]
    return {
        "rows": [{"workcell": r.workcell, "mes_step": r.name_raw,
                  "process_key": r.process_key, "status": r.status,
                  "iedb_alias": r.iedb_alias, "source": r.source,
                  "decided_by": r.decided_by, "decided_on": r.decided_on,
                  "models": int(r.models), "rows": int(r.rows), "scans": int(r.scans)}
                 for r in out.itertuples()],
        "total": total, "page": page, "page_size": page_size, "counts": counts,
    }


def steps(workcell: str, q: str = "") -> list[dict]:
    """Back-compat shim for /registry/steps. `process_list` is the one
    implementation — two copies of the resolution order is how the same step
    ends up with two different verdicts on two pages."""
    return process_list(scope="scanned", workcell=workcell, q=q,
                        page_size=1_000_000)["rows"]


def decide(workcell: str, mes_step: str, answer: str, iedb_alias: str | None,
           evidence: str | None, ntid: str) -> dict:
    """Record one answer. Re-deciding replaces — there is one live answer per step."""
    if answer not in ("mapped", "non_iedb", "unknown"):
        raise ValueError(f"answer must be mapped|non_iedb|unknown, got {answer!r}")
    if answer == "mapped" and not (iedb_alias or "").strip():
        raise ValueError("answer='mapped' needs an iedb_alias")
    if answer != "mapped":
        iedb_alias = None
    with get_conn() as c:
        c.execute("""
            INSERT INTO process_decision
                (workcell, mes_step, answer, iedb_alias, evidence, decided_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (workcell, mes_step) DO UPDATE SET
                answer     = excluded.answer,
                iedb_alias = excluded.iedb_alias,
                evidence   = excluded.evidence,
                decided_by = excluded.decided_by,
                decided_on = datetime('now')
        """, (workcell, mes_step, answer, iedb_alias, evidence, ntid))
    log.info("process_decision: %s / %r -> %s %s (by %s)",
             workcell, mes_step, answer, iedb_alias or "", ntid)
    return {"workcell": workcell, "mes_step": mes_step, "answer": answer,
            "iedb_alias": iedb_alias, "decided_by": ntid}


def decide_bulk(items: list[dict], ntid: str) -> dict:
    """Answer many steps at once.

    `MI TOP`, `MI TOP 1`, `MI_TOP` and `MI_TOP 1` are four rows and one answer.
    Made one at a time that is four round-trips and four chances to typo the
    alias; the whole page exists because 3s per step and 30s per step are the
    difference between finishing and not.

    All-or-nothing on purpose: a half-applied bulk edit leaves the operator
    guessing which rows took, and re-running it is not safe if some did.
    """
    if not items:
        return {"saved": 0}
    clean = []
    for it in items:
        answer = str(it.get("answer", "")).strip()
        alias = (it.get("iedb_alias") or "").strip() or None
        if answer not in ("mapped", "non_iedb", "unknown"):
            raise ValueError(f"answer must be mapped|non_iedb|unknown, got {answer!r}")
        if answer == "mapped" and not alias:
            raise ValueError("answer='mapped' needs an iedb_alias")
        if answer != "mapped":
            alias = None
        wc = str(it.get("workcell", "")).strip()
        step = str(it.get("mes_step", ""))          # byte-exact: spaces matter
        if not wc or not step:
            raise ValueError("each item needs workcell and mes_step")
        clean.append((wc, step, answer, alias, it.get("evidence"), ntid))

    with get_conn() as c:
        c.executemany("""
            INSERT INTO process_decision
                (workcell, mes_step, answer, iedb_alias, evidence, decided_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (workcell, mes_step) DO UPDATE SET
                answer     = excluded.answer,
                iedb_alias = excluded.iedb_alias,
                evidence   = excluded.evidence,
                decided_by = excluded.decided_by,
                decided_on = datetime('now')
        """, clean)
    log.info("process_decision bulk: %d step(s) by %s", len(clean), ntid)
    return {"saved": len(clean)}


def aliases(workcell: str) -> list[dict]:
    """This workcell's own IEDB processes — the pick-list for 'mapped'.

    Scoped to the workcell on purpose. `MA 1` is Mech Assy at ARISTA, Smart
    Torque at BD and Deposition OPT 10 at LAM GAS BOX; offering the plant-wide
    list would invite exactly the cross-workcell mix-up the registry exists to
    prevent.

    BOTH IEDB NAMES, not just the alias.
      alias    'POT 1'      the identifier, and what gets stored
      process  'Potting 1'  IEDB's display name for the same step

    The alias alone is often unreadable — `TSTH 1`, `FNI VIP 1`, `BSI 1` — and
    the person answering "is MES's `CHEMICAL WASH 1 LINK` this one?" was being
    shown half the evidence IEDB actually holds. `process` is never a match key
    (matching on it manufactured 1,377 false 'present' verdicts, because it is
    free text) but as a LABEL it is exactly what makes the alias legible.

    Returns [{alias, process, models}] worst-known-first: an alias nobody has
    mapped yet, and that many models use, is the one worth reading carefully.
    """
    raw = CT_MART["raw"]
    if not raw.exists():
        return []
    con = duckdb.connect()
    try:
        df = con.execute(
            f"""
            SELECT alias,
                   -- One alias can carry several display names across models.
                   -- Keep them all, most common first; a disagreement between
                   -- them is itself evidence worth seeing.
                   string_agg(DISTINCT process, ' / ') AS process,
                   COUNT(DISTINCT assembly)            AS models
            FROM read_parquet('{raw.as_posix()}')
            WHERE regexp_replace(upper(customer), '[^A-Z0-9]', '', 'g') = ?
              AND alias IS NOT NULL AND trim(alias) <> ''
            GROUP BY alias
            ORDER BY models DESC, alias
            """,
            [_wcnorm(workcell)],
        ).df()
    finally:
        con.close()
    return [{"alias": a, "process": p or "", "models": int(m)}
            for a, p, m in zip(df["alias"], df["process"], df["models"])]


@lru_cache(maxsize=2)
def _models(_key: float) -> pd.DataFrame:
    """The IEDB catalogue — 350k rows, so cached on the file's mtime like the
    rest of the module rather than re-read per keystroke."""
    p = CT_MART["assembly_catalog"]
    return pd.read_parquet(p, columns=["customer", "assembly", "revision",
                                       "description", "has_data"])


def search(q: str, limit: int = 8) -> dict:
    """One box, three kinds of answer: workcell, model, process.

    Someone arriving at Cycle Time knows a part number, or a workcell, or the
    name of a step they saw on the floor — and today has to know WHICH of those
    it is before they can look for it. This does not make them choose.

    Matches are substring, case- and punctuation-insensitive, because the whole
    point of the registry is that nobody spells these the same way twice.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return {"query": q, "workcells": [], "models": [], "processes": []}
    needle = _wcnorm(q)
    proc, _ = _frames()

    wcs = sorted({w for w in proc["workcell"] if needle in _wcnorm(w)})

    # processes: match the key, the display name, or EITHER system's spelling —
    # the user may have read the name off a MES screen or an IEDB sheet
    p = proc[proc.apply(lambda r: any(
        needle in _wcnorm(r[c]) for c in
        ("process_key", "process_name", "iedb_aliases", "mes_steps")), axis=1)]
    p = p.sort_values("mes_scans", ascending=False).head(limit)

    try:
        m = _models(CT_MART["assembly_catalog"].stat().st_mtime)
        hit = m[m["assembly"].astype(str).str.upper().str.contains(q.upper(), regex=False)]
        models = [{"workcell": r["customer"], "assembly": r["assembly"],
                   "revision": r["revision"], "description": r["description"],
                   "has_data": bool(r["has_data"])}
                  for _, r in hit.head(limit).iterrows()]
    except Exception as e:                                  # never break the box
        log.warning("model search unavailable: %s", e)
        models = []

    return {
        "query": q,
        "workcells": [{"workcell": w} for w in wcs[:limit]],
        "models": models,
        "processes": [{"workcell": r["workcell"], "process_key": r["process_key"],
                       "process_name": r["process_name"], "source": r["source"],
                       "iedb_aliases": r["iedb_aliases"], "mes_steps": r["mes_steps"],
                       "mes_scans": int(r["mes_scans"] or 0)}
                      for _, r in p.iterrows()],
    }


def export_decisions(path=None) -> int:
    """SQLite -> process_decision.csv, the file the registry generators read.

    Written in the exact shape `_registry_keys.load_bridge()` expects, so a
    generator re-run picks the answers up with no further step. 'unknown' rows
    are skipped: they are a record that someone looked, not a mapping."""
    path = path or (REG_DIR / "process_decision.csv")
    with get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT workcell, mes_step, answer, iedb_alias, evidence, decided_by,"
            " decided_on FROM process_decision WHERE answer <> 'unknown'"
            " ORDER BY workcell, mes_step")]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["workcell", "mes_step", "answer",
                                           "iedb_alias", "evidence", "decided_by",
                                           "decided_on"])
        w.writeheader()
        for r in rows:
            r["iedb_alias"] = r["iedb_alias"] or ""
            w.writerow(r)
    log.info("exported %d decisions -> %s", len(rows), path)
    return len(rows)
