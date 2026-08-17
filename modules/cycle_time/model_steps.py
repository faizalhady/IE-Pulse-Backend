"""
model_steps.py  (cycle_time)
────────────────────────────
The configured step list for one model — the last join in the domain.

    mes_route_master     model  -> factory / bay / route      (330,068 rows)
    mes_process_master   route  -> ordered steps              ( 91,010 rows)
    ------------------------------------------------------------------------
    this module          model  -> bay -> ordered steps

WHY THIS IS NOT "model -> steps"
  A model does not have a route. It has ONE ROUTE PER BAY. E5052-61032 is
  qualified on 227 bays and carries 966 routes; the median model has 3 routes
  across 2 bays. So "what steps does this model go through?" has no single
  answer — it depends on where it is built, and any code that picks one route
  arbitrarily is inventing a fact.

  The bay is therefore part of the key, exposed to the caller, never collapsed.

WHY `fma_route_id` AND NOT `route_name`
  `route_name` repeats across factories ('SUPERCELL ROUTE B203' exists in more
  than one place), so joining on it fuses steps from different plants into one
  list. `fma_route_id` is MES's own factory+MA+route identity and cannot.

  It costs coverage: 1,198 of our 1,818 route ids appear in the step master
  (66%), against an apparent 89% on the name. The name's extra 23% is false
  matching, not extra data.

DEAD ROUTES — 620 of them, and they are MES's, not ours
  MES's assembly records point at 620 route ids that do not exist in its own
  route table. 66,538 assembly-route rows, 20% of the file. `KEYSIGHT RMA
  ROUTE 01` (id 2095, factory P1) is one.

  Proven, not assumed:
    * a control route returns 689 steps — endpoint and params work
    * 12 of 12 sampled dead ids return 404
    * 5 param variations on a dead id all fail
    * pulling `factory=P1` explicitly (where most of them live) returns
      55,431 rows and ZERO routes we did not already have — so the `%`
      wildcard was not truncating

  They are surfaced, never silently dropped. A model showing 3 of its 5 bays
  must say why the other 2 are absent, or it reads as OUR data being missing.

WHAT THIS DOES NOT ANSWER
  Whether the model ACTUALLY ran those steps. This is the configured route —
  what MES says can happen. Scan history (`completion_steps_v2`) says what did.
  The difference between them is the point: a step configured but never scanned
  in 24 months is either dead config or a process that quietly stopped.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

import duckdb
import pandas as pd

from modules.cycle_time.config import CT_MART

log = logging.getLogger(__name__)

ROUTES = CT_MART["raw"].parent / "mes_route_master.parquet"
STEPS = CT_MART["raw"].parent / "mes_process_master.parquet"

_norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def available() -> bool:
    return ROUTES.exists() and STEPS.exists()


@lru_cache(maxsize=256)
def for_model(assembly: str, revision: str | None = None) -> list[dict]:
    """Every (bay, route) this model is configured for, each with its steps.

    -> [{factory, bay, route, fma_route_id, revision, steps:[{order, step, instance,
         type, workcenter}]}]

    Sorted by step count descending: the fullest route first, because a model
    qualified on 227 bays is usually asking "what is the real one?" and the
    fullest is the best first guess. It is a GUESS, and the caller sees all of
    them rather than being handed one.
    """
    if not available():
        return []
    con = duckdb.connect()
    try:
        # LEFT JOIN, not inner. An inner join drops the dead routes entirely, so
        # a model qualified on 5 bays quietly shows 3 and the reader concludes we
        # are missing data. The row survives with `alive = false` and no steps.
        rows = con.execute(
            f"""
            SELECT r.fma_route_id, r.revision, r.factory_name AS factory,
                   r.manufacturing_area_name AS bay, r.route_name AS route,
                   s.step_order, s.step_name, s.description, s.step_type_name,
                   s.work_center_text,
                   s.factory_m_a_route_i_d IS NOT NULL AS alive
            FROM read_parquet('{ROUTES.as_posix()}') r
            LEFT JOIN read_parquet('{STEPS.as_posix()}') s
              ON r.fma_route_id = s.factory_m_a_route_i_d
            WHERE regexp_replace(upper(r.number), '[^A-Z0-9]', '', 'g') = ?
              {"AND r.revision = ?" if revision else ""}
            ORDER BY r.fma_route_id, s.step_order
            """,
            [_norm(assembly)] + ([revision] if revision else []),
        ).df()
    finally:
        con.close()
    if rows.empty:
        return []

    out = []
    for (fid, rev, fac, bay, route), g in rows.groupby(
            ["fma_route_id", "revision", "factory", "bay", "route"], sort=False):
        alive = bool(g["alive"].any())
        out.append({
            "fma_route_id": int(fid), "revision": rev, "factory": fac,
            "bay": bay, "route": route,
            "step_count": int(len(g)) if alive else 0,
            # False = MES's assembly record points at a route its own route table
            # does not have. Nothing to fetch; the reference is broken upstream.
            "alive": alive,
            "note": "" if alive else "MES has no route record for this id - dead reference",
            "steps": [] if not alive else [
                # BOTH MES name levels, always. `step_name` is the general step
                # ('AOI'), `description` the instance ('AOI TOP') — and the
                # instance is the one that joins to an IEDB alias. Carrying only
                # one of them is what made `MA 1` look like a single process
                # across three workcells.
                {"order": int(o) if pd.notna(o) else None,
                 "step": str(sn or "").strip(),
                 "instance": str(d or "").strip(),
                 "type": str(t or "").strip(),
                 "workcenter": str(w or "").strip()}
                for o, sn, d, t, w in zip(g["step_order"], g["step_name"],
                                          g["description"], g["step_type_name"],
                                          g["work_center_text"])],
        })
    # Alive first, fullest first. Dead routes sink to the bottom but never
    # disappear — the reader must be able to see that MES lost them.
    return sorted(out, key=lambda r: (not r["alive"], -r["step_count"]))


def coverage() -> dict:
    """How many models can now be given a configured step list, and how many
    cannot — with the reason. Printed rather than assumed."""
    if not available():
        return {}
    con = duckdb.connect()
    try:
        return con.execute(
            f"""
            WITH r AS (SELECT DISTINCT number, fma_route_id
                       FROM read_parquet('{ROUTES.as_posix()}')),
                 s AS (SELECT DISTINCT factory_m_a_route_i_d AS fid
                       FROM read_parquet('{STEPS.as_posix()}'))
            SELECT COUNT(DISTINCT r.number)                                    AS models_with_route,
                   COUNT(DISTINCT CASE WHEN s.fid IS NOT NULL THEN r.number END) AS models_with_steps,
                   COUNT(DISTINCT r.fma_route_id)                              AS routes,
                   COUNT(DISTINCT CASE WHEN s.fid IS NOT NULL THEN r.fma_route_id END) AS routes_alive,
                   COUNT(DISTINCT CASE WHEN s.fid IS NULL     THEN r.fma_route_id END) AS routes_dead,
                   COUNT(DISTINCT CASE WHEN s.fid IS NULL     THEN r.number END)       AS models_touching_a_dead_route
            FROM r LEFT JOIN s ON r.fma_route_id = s.fid
            """).df().to_dict("records")[0]
    finally:
        con.close()


def _selfcheck() -> None:
    c = coverage()
    print("coverage:", {k: f"{v:,}" for k, v in c.items()})
    assert c["models_with_steps"] > 0, "the join produced nothing - check fma_route_id"
    # A dead route must SURVIVE the join. Dropping it is the failure this flag
    # exists to prevent, and it would look like our data being incomplete.
    assert c["routes_dead"] > 0, "expected dead routes to be counted, not dropped"
    # The bay must survive as its own dimension; collapsing it is the one thing
    # this module exists to prevent.
    con = duckdb.connect()
    worst = con.execute(
        f"""SELECT number FROM read_parquet('{ROUTES.as_posix()}')
            GROUP BY number ORDER BY COUNT(DISTINCT manufacturing_area_name) DESC LIMIT 1"""
    ).fetchone()[0]
    con.close()
    r = for_model(worst)
    bays = {x["bay"] for x in r}
    print(f"widest model {worst!r}: {len(r)} routes across {len(bays)} bays")
    assert len(bays) > 1 or len(r) <= 1, "bays were collapsed"
    print("selfcheck OK - bay survives as its own dimension")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
    _selfcheck()
