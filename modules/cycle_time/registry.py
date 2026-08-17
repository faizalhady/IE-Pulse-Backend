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

    g["answered"] = g["name_raw"].map(lambda s: s in answered)
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


def steps(workcell: str, q: str = "") -> list[dict]:
    """EVERY MES step this workcell scans, with its current mapping and where
    that mapping came from.

    `questions()` only serves the unmapped. That left a wrong mapping permanent:
    if the workbook says `POST SOLDER INSP 2 -> MSOLDER 2` and it is wrong,
    nothing could correct it. This is the editable view — a mapping is a
    decision someone made, and decisions get revised.

    `source` says who is answering:
        decision  an engineer said so here      (overrides the workbook)
        workbook  the hand-typed Excel sheet
        auto      the plant-wide process_type id, used when nothing else knows
        none      nothing maps it - this is what `questions()` serves
    """
    raw_path = REG_DIR / "workcell_process_raw.csv"
    if not raw_path.exists():
        return []
    df = pd.read_csv(raw_path).fillna("")
    g = df[(df["system"] == "mes_step")
           & (df["workcell"].map(_wcnorm) == _wcnorm(workcell))].copy()

    with get_conn() as c:
        decided = {r["mes_step"]: dict(r) for r in c.execute(
            "SELECT * FROM process_decision WHERE workcell = ?", (workcell,))}
    # the workbook, so a mapping can say where it came from
    from modules.cycle_time.config import CT_MART
    book = set()
    try:
        pm = pd.read_parquet(CT_MART["mes_process_map"])
        book = {(_wcnorm(c), re.sub(r"\s+", " ", str(s).strip().upper()))
                for c, s in zip(pm["customer"], pm["step_instance"])}
    except Exception:                                   # never block the view
        pass

    if q:
        needle = q.strip().lower()
        g = g[g.apply(lambda r: needle in str(r["name_raw"]).lower()
                      or needle in str(r["process_key"]).lower(), axis=1)]

    g["scans"] = pd.to_numeric(g["scans"], errors="coerce").fillna(0)
    g = g.sort_values("scans", ascending=False)

    out = []
    for _, r in g.iterrows():
        d = decided.get(r["name_raw"])
        key = (_wcnorm(workcell), re.sub(r"\s+", " ", str(r["name_raw"]).strip().upper()))
        src = ("decision" if d else
               "workbook" if key in book else
               "auto" if r["process_key"] else "none")
        out.append({
            "workcell": r["workcell"], "mes_step": r["name_raw"],
            "process_key": r["process_key"], "answer": r["answer"],
            "models": int(pd.to_numeric(r.get("models"), errors="coerce") or 0),
            "scans": int(r["scans"]),
            "source": src,
            "iedb_alias": (d or {}).get("iedb_alias") or "",
            "decided_by": (d or {}).get("decided_by") or "",
            "decided_on": (d or {}).get("decided_on") or "",
        })
    return out


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
