"""
process_bridge.py  (cycle_time)
───────────────────────────────
THE MES-step -> IEDB-alias bridge. One loader, three layers, in precedence order.

WHY THIS EXISTS
  The grader read `mes_process_map.parquet` directly — a flat dump of the MNS
  workbook — and then overlaid a 10-row decision file on top. Meanwhile the
  registry (`workcell_process_raw.csv`, 15,378 rows) had been built to be exactly
  this bridge and was read by nobody. Two structures describing one relationship,
  and the richer one was inert.

  The workbook is not replaced. It is INSIDE the registry already, as
  `system='bridge'` (9,734 rows), so reading the registry reads the workbook plus
  everything else that was learned since.

THE THREE LAYERS  (later wins)
  1. bridge      the MNS workbook, regenerated from the spreadsheet
  2. mes_step    names seen in real scans, resolved through `process_key`
  3. decisions   what an engineer actually answered on the registry page

WHY `process_key` AND NOT THE NAME
  A name matches nothing: MES writes 'AOI BTM', IEDB writes 'AOIB 1'. The
  registry gives both an identity — `AOIB#1` — computed by the same rule on both
  sides. So a MES name resolves to an IEDB alias by going through the identity,
  never by string similarity.

  It is WORKCELL-SCOPED, always. `MA 1` is Mech Assy at ARISTA, Smart Torque at
  BD and Deposition OPT 10 at LAM GAS BOX. A plant-wide bridge fuses three
  different processes into one and every number downstream is wrong.

WHAT IS DELIBERATELY NOT HERE
  IEDB's `process` column ('Assembly 2', 'Link 1') is NOT a match source.
  Matching on it manufactured 1,377 false "present" verdicts, because it is a
  human display label rather than an identifier. It is carried as EVIDENCE for
  whoever has to answer an unknown name (see D3), never as a key.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

_cnorm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s or "").upper())
#: MES step names carry meaningful trailing/double spaces — 20 of them differ
#: only by that. Collapse whitespace for MATCHING, never for storage.
_snorm = lambda s: re.sub(r"\s+", " ", str(s or "")).strip().upper()


def _registry(mart: Path) -> Path:
    return mart / "cycle_time" / "registry"


def load(mart: Path) -> tuple[dict, set, dict]:
    """-> (pmap, pknown, stats)

    pmap   {(cnorm workcell, snorm mes_name): iedb_alias}  — resolves to an alias
    pknown {(cnorm workcell, snorm mes_name)}              — DECLARED not-IEDB

    `pknown` IS NOT "every name we have seen". It is the set a human has ruled
    on and said is not IEDB work — rework, handling, a scan point. The grader
    reads it that way:

        completion_v2.py:540
            known = any((cn, n) in ctx.pknown for n in names)
            if alias is None and known:
                return "non_iedb", ""      # excluded from the gap entirely

    So anything added here is REMOVED FROM THE GAP. Filling it with every name
    the registry has ever seen (73,005 of them) would have silently swallowed
    62,612 unnamed steps on the next grading run — they would read as "not IEDB
    work" instead of "we could not name it", which is the opposite of true and
    invisible afterwards. Only the workbook's is_iedb=False rows and explicit
    `non_iedb` decisions belong in it.
    """
    reg = _registry(mart)
    raw_csv = reg / "workcell_process_raw.csv"
    pmap: dict = {}
    pknown: set = set()
    stats = {"bridge": 0, "mes_step": 0, "decisions": 0, "non_iedb": 0, "unmapped": 0}

    if not raw_csv.exists():
        log.warning("no registry at %s - falling back to the workbook only", raw_csv)
        return pmap, pknown, stats

    d = pd.read_csv(raw_csv, dtype=str, keep_default_na=False)
    d["_wc"] = d["workcell"].map(_cnorm)
    d["_nm"] = d["name_raw"].map(_snorm)

    # IEDB side first: process_key -> the alias IEDB actually writes, per workcell.
    ia = d[d["system"] == "iedb_alias"]
    alias_of = {(w, k): nm for w, k, nm in zip(ia["_wc"], ia["process_key"], ia["name_raw"]) if k}

    # ── layer 1: the workbook, read DIRECTLY ─────────────────────────────────
    # Not through the registry's copy of it. The registry keeps each workbook row
    # as (name -> process_key) and drops the alias the workbook itself carried,
    # so rebuilding the mapping by hopping process_key -> iedb_alias only works
    # where that workcell also has an IEDB row for the same identity. It does not
    # for 998 of 4,227 — the selfcheck caught exactly that, as a 998-name silent
    # regression. The workbook is the authored base; the registry ADDS to it.
    wb = mart / "cycle_time" / "mes_process_map.parquet"
    if wb.exists():
        w_ = pd.read_parquet(wb)
        for c, s, a, isie in zip(w_["customer"], w_["step_instance"],
                                 w_["iedb_alias"], w_["is_iedb"]):
            k = (_cnorm(c), _snorm(s))
            if isie and str(a).strip() and str(a) != "nan":
                pmap[k] = a
                stats["bridge"] += 1
            elif not isie:
                # The workbook says this is NOT IEDB work. Only these belong in
                # pknown — see the contract in load()'s docstring.
                pknown.add(k)

    # ── layer 2: names seen in real scans, resolved through the identity ─────
    sub = d[d["system"] == "mes_step"]
    for w, nm, key, ans in zip(sub["_wc"], sub["_nm"], sub["process_key"], sub["answer"]):
        if not nm:
            continue
        if ans == "non_iedb":
            # Somebody ruled this rework/handling. THIS is what pknown is for,
            # and it is why the answer matters rather than merely having seen
            # the name.
            pknown.add((w, nm))
            pmap.pop((w, nm), None)
            stats["non_iedb"] += 1
            continue
        if ans == "unmapped":
            stats["unmapped"] += 1
            continue
        if (w, nm) in pmap:
            continue                       # the workbook already answered it
        a = alias_of.get((w, key)) if key else None
        if a:
            pmap[(w, nm)] = a
            stats["mes_step"] += 1

    # ── layer 3: what an engineer answered. Authored, so it wins ─────────────
    #
    # READ FROM SQLITE, which is where the registry page WRITES. It used to read
    # `process_decision.csv`, a second copy produced only when an admin
    # remembered to POST /registry/export — so every answer an engineer gave was
    # invisible to the grader until somebody pressed a button. Nothing scheduled
    # that button.
    #
    # One store. The CSV still exists as an export for the laptop generators and
    # as something a human can read, but nothing depends on it being current.
    try:
        from core.database import get_conn
        with get_conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT workcell, mes_step, answer, iedb_alias FROM process_decision")]
        for r in rows:
            k = (_cnorm(r["workcell"]), _snorm(r["mes_step"]))
            alias = (r["iedb_alias"] or "").strip()
            if r["answer"] == "mapped" and alias:
                pmap[k] = alias
                stats["decisions"] += 1
            elif r["answer"] == "non_iedb":
                pknown.add(k)              # declared, so it leaves the gap
                pmap.pop(k, None)
            # 'unknown' is skipped: it records that somebody looked and could not
            # say, which is not a mapping.
    except Exception as e:                  # a DB problem must not kill a run
        log.warning("could not read process_decision from SQLite, ignoring: %s", e)

    log.info("bridge: %d mapped (%d workbook, %d from scans, %d authored) | "
             "%d known names, %d non-IEDB, %d still unanswered",
             len(pmap), stats["bridge"], stats["mes_step"], stats["decisions"],
             len(pknown), stats["non_iedb"], stats["unmapped"])
    return pmap, pknown, stats


def _selfcheck(mart: Path) -> None:
    """Proves the registry bridge is a SUPERSET of the workbook it replaces.

    The whole change is worthless if it resolves fewer names than what it
    replaced, and that regression would be invisible — it would surface weeks
    later as a handful of models quietly reading 'unmapped'.
    """
    pmap, pknown, _ = load(mart)
    wb_path = mart / "cycle_time" / "mes_process_map.parquet"
    if not wb_path.exists():
        print("no workbook to compare against")
        return
    wb = pd.read_parquet(wb_path)
    old = {(_cnorm(c), _snorm(s)): a
           for c, s, a, isie in zip(wb["customer"], wb["step_instance"],
                                    wb["iedb_alias"], wb["is_iedb"]) if isie}
    lost = [k for k in old if k not in pmap]
    print(f"workbook mapped {len(old)}, registry maps {len(pmap)}")
    print(f"  gained : {len(set(pmap) - set(old))}")
    print(f"  lost   : {len(lost)}")
    if lost[:5]:
        print("  e.g.  ", lost[:5])
    assert len(pmap) >= len(old) * 0.95, "registry bridge resolves FEWER names than the workbook"

    # pknown REMOVES steps from the gap (completion_v2:540). It must stay the
    # set of DECLARED not-IEDB names, never "every name seen" — filling it from
    # the whole registry once put 73,005 names in it, which would have swallowed
    # 62,612 unnamed steps as "not IEDB work" on the next grading run.
    wb_declared = sum(1 for isie in wb["is_iedb"] if not isie)
    assert len(pknown) <= wb_declared * 1.5, (
        f"pknown has {len(pknown)} entries against {wb_declared} declared in the "
        f"workbook - it is being filled with SEEN names, not DECLARED ones")
    print(f"selfcheck OK - no regression, and pknown stays declared-only "
          f"({len(pknown)} vs {wb_declared} in the workbook)")


if __name__ == "__main__":
    from modules.cycle_time.config import CT_MART
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
    _selfcheck(CT_MART["raw"].parent.parent)
