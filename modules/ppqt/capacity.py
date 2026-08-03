"""
modules/ppqt/capacity.py
────────────────────────
PPQT capacity sizing, computed forward from real data.

The chain (docs/PPQT_BUILD.md §4):

    Total_adj_CT  =  mach/cap × S%  +  imt × S%  +  hand × S%
    WCT_process   =  Σ(demand × CT) / Σ(demand)        [CT > 0 only]
    Available_t   =  (shift_hrs×60 − co_qty×co_time) × days × 60   [seconds]
    Takt          =  Available_t / demand_through_process
    Need          =  ROUNDUP( WCT / (Takt × FPY × Eff) )
    Gap           =  Need − eq_avail
    Util%         =  WCT / Takt / FPY / Eff / eq_avail × 100

Inputs, all real:
  • cycle time  — data/mart/cycle_time/raw.parquet   (4.4M rows, IEDB master)
  • demand      — data/mart/demand/planner_demand.parquet  (planner, 13wk window)

Notes on the source data (verified 2026-07-22):
  • `cycle_time_per_process` in raw.parquet ALREADY equals Total_adj_CT.
    Verified: mach=37.67, hand=17.61, cap=4, S=100 → 37.67/4 + 17.61 = 27.0275.
    We use it directly rather than recomputing §4.1.
  • `sampling` is the S% column. 4,455,537 of 4,455,785 rows are 100.
  • `fpy` mixes percent (99.0) and fraction (0.99, ~64.6k rows) — normalised below.
  • `quote` is effectively empty (one non-null row). The spec's "filter Quote==99"
    is stale; we do not filter on it.
  • Workcell names differ between the two marts (BECKMANCOULTER vs BC (DANAHER),
    TMO vs THERMO FISHER, UTAS vs COLLINS, ...). We join on normalised ASSEMBLY
    only and take the workcell from the cycle-time side — each demand workcell
    resolves to exactly one CT workcell. Joining on workcell drops ~1.9M units.
"""

from __future__ import annotations

import math
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

import duckdb
import pandas as pd

from core.paths import DATA_MART_DIR

CT_RAW = DATA_MART_DIR / "cycle_time" / "raw.parquet"
PLANNER_DEMAND = DATA_MART_DIR / "demand" / "planner_demand.parquet"

# Normalise a name for joining: uppercase, strip everything non-alphanumeric.
_NORM = r"regexp_replace(upper(trim({col})), '[^A-Z0-9]', '', 'g')"

# ponytail: shift params are not in any mart yet (they live in the SBWC sheet of
# the per-customer PPQT workbook). Defaults here, overridable per request.
# Upgrade path: ingest SBWC → data/mart/ppqt/shift.parquet, look up per sub-wc.
#
# PPQT is a MONTHLY analysis. Confirmed against the source of truth:
#   • Training Package 6.0 slide 7: "It is recommended to use the Monthly Demand."
#   • Slide 8: "Shift Patterns and working hours per month."
#   • PPQT (Wabtec customer).xlsx, DASH!C22: "Days in the Period of the Demand = 26"
#   • DMAN holds ONE scalar demand per assembly — one workbook = one period.
# 26 days = a month of 6-day weeks, matching the Wabtec sheet exactly.
DEFAULTS = {
    "shift_hours": 21.0,     # hours of line uptime per day
    "working_days": 26.0,    # one month of 6-day weeks (DASH!C22)
    "co_qty": 4.0,           # changeovers per day
    "co_time": 20.0,         # minutes per changeover
    "efficiency": 85.0,      # %
    "eq_avail": 1,           # machines available per process — see docstring
}

# A per-unit CT above this can't be a serial station operation — it's a batch /
# dwell time (burn-in, ESS, FVT, soak, wave solder) recorded as the whole-oven
# time with no parallelism (the IEDB `cap` column is 1 for these). PPQT's serial
# formula would size it as if each unit occupies the station back-to-back and
# demand hundreds of ovens/operators. So we FLAG these steps `dwell` and leave
# them out of the shortfall/utilisation rollups for an IE to size by hand.
# 182 of 2,818 steps exceed this today (burn-in reaches 8 days/unit).
# ponytail: 1h ceiling is a calibration knob — raise it if a real serial station
# legitimately runs longer than an hour per unit. Upgrade path: ingest true
# units-per-batch and divide dwell CT down to per-unit instead of flagging.
DWELL_CT_CEILING_SEC = 3600.0

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def default_month() -> str:
    """Current calendar month — the period a PPQT run defaults to."""
    return date.today().strftime("%Y-%m")


def _resolve_month(month: str | None) -> str:
    """
    Validate a YYYY-MM string before it reaches SQL, defaulting to this month.

    Always returns a concrete month — PPQT is a monthly analysis and nothing
    should silently aggregate every period. Centralised so a new query can't
    forget to default (the TOP Demand matrix once did, and summed all periods
    while the rest of the page showed one month).
    """
    if month is None or month == "":
        return default_month()
    if not _MONTH_RE.match(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    return month


def _dem_sql(month: str) -> str:
    """
    Demand per assembly for one calendar month.

    Weekly rows are attributed to the month their period_start falls in; no
    workcell mixes week and month rows (verified), so summing both is safe.
    """
    where = f"WHERE strftime(period_start, '%Y-%m') = '{month}'"
    return f"""
        SELECT {_NORM.format(col='model')} AS asm, SUM(qty) AS demand
        FROM read_parquet('{PLANNER_DEMAND.as_posix()}')
        {where}
        GROUP BY 1 HAVING SUM(qty) > 0
    """


def list_months() -> list[str]:
    """Months that have planner demand, ascending — populates the period selector."""
    _require(PLANNER_DEMAND, "planner demand")
    con = _con()
    try:
        rows = con.execute(f"""
            SELECT DISTINCT strftime(period_start, '%Y-%m') AS ym
            FROM read_parquet('{PLANNER_DEMAND.as_posix()}')
            WHERE qty > 0
            ORDER BY 1
        """).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]

def _area(sub_workcenter: str) -> str:
    """Classify a line into SMT / TH / BE from its name. Falls back to BE."""
    t = f" {sub_workcenter.upper()} "
    if " SMT " in t:
        return "SMT"
    if " TH " in t:
        return "TH"
    return "BE"


def _slug(s: str) -> str:
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", s.lower()))


def available_seconds(shift_hours: float, working_days: float,
                      co_qty: float, co_time: float) -> float:
    """§4.3 — productive seconds in the demand period."""
    minutes_per_day = shift_hours * 60.0 - co_qty * co_time
    return max(0.0, minutes_per_day) * working_days * 60.0


def _con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    return con


def _require(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} mart not found: {path}")


def list_workcells(month: str | None = None) -> pd.DataFrame:
    """Workcells that have BOTH cycle time and planner demand. For the league table."""
    _require(CT_RAW, "cycle time")
    _require(PLANNER_DEMAND, "planner demand")
    month = _resolve_month(month)

    con = _con()
    try:
        return con.execute(f"""
            WITH dem AS (
                SELECT asm, demand AS qty FROM ({_dem_sql(month)})
            ),
            ct AS (
                -- NB: raw.parquet's `division` column is a coded customer tag
                -- ("KEYSIGHT*", "TMO*"), not a business division. Not surfaced.
                SELECT DISTINCT workcell, {_NORM.format(col='assembly')} AS asm
                FROM read_parquet('{CT_RAW.as_posix()}')
                WHERE workcell IS NOT NULL
            )
            SELECT ct.workcell            AS workcell,
                   COUNT(*)               AS assemblies,
                   CAST(SUM(dem.qty) AS BIGINT) AS total_demand
            FROM dem JOIN ct USING (asm)
            GROUP BY 1
            HAVING SUM(dem.qty) > 0
            ORDER BY total_demand DESC
        """).df()
    finally:
        con.close()


def _process_rows(workcell: str, month: str | None = None) -> pd.DataFrame:
    """
    Join demand × cycle time for one workcell, aggregated to
    (sub_workcenter, process). Returns the raw ingredients for the forward chain.
    """
    _require(CT_RAW, "cycle time")
    _require(PLANNER_DEMAND, "planner demand")
    month = _resolve_month(month)

    con = _con()
    try:
        return con.execute(f"""
            WITH dem AS ({_dem_sql(month)}),
            ct AS (
                SELECT {_NORM.format(col='assembly')} AS asm,
                       assembly, revision, sub_workcenter, process,
                       mach, imt, hand, cap, sampling, hc,
                       -- fpy arrives as both 99.0 and 0.99 — normalise to percent
                       CASE WHEN fpy IS NULL THEN NULL
                            WHEN fpy <= 1.0 THEN fpy * 100.0
                            ELSE fpy END AS fpy,
                       -- `type` is the CT-source marker: Stop Watch / Estimate / MOST
                       CASE WHEN type = 'Estimate'   THEN 'Est'
                            WHEN type = 'MOST'       THEN 'MOST'
                            WHEN type = 'Stop Watch' THEN 'SW'
                            ELSE 'Est' END AS ct_source,
                       cycle_time_per_process AS ct_adj,
                       -- Manual-only CT for DL sizing: operator time, no machine
                       -- time. ct_adj = mach/cap*S + imt*S + hand*S, so the manual
                       -- half is simply (imt + hand) * S. Slide 16: PF&D 15% is
                       -- already inside the manual time study.
                       (COALESCE(imt, 0) + COALESCE(hand, 0))
                         * COALESCE(sampling, 100) / 100.0 AS manual_adj
                FROM read_parquet('{CT_RAW.as_posix()}')
                WHERE workcell = ?
                  AND sub_workcenter IS NOT NULL
                  AND process IS NOT NULL
                  AND cycle_time_per_process > 0
            ),
            -- Collapse to ONE cycle time per (assembly, sub_wc, process) BEFORE
            -- joining demand. raw.parquet holds a row per revision, so joining
            -- demand first would count each assembly's demand once per revision.
            asm_ct AS (
                SELECT asm, sub_workcenter, process,
                       MEDIAN(ct_adj)     AS ct_adj,
                       MEDIAN(manual_adj) AS manual_adj,
                       MEDIAN(fpy)        AS fpy,
                       MEDIAN(hc)         AS hc,
                       MODE(ct_source)    AS ct_source
                FROM ct
                GROUP BY 1, 2, 3
            )
            SELECT a.sub_workcenter,
                   a.process,
                   COUNT(*)                          AS assembly_count,
                   -- Equipment side: every assembly routing through the step.
                   CAST(SUM(dem.demand) AS DOUBLE)   AS demand_through,
                   SUM(dem.demand * a.ct_adj)        AS demand_x_ct,
                   MEDIAN(a.ct_adj)                  AS median_ct,
                   -- People side: only assemblies that need an operator here.
                   -- A machine-only step has no manual time, so no DL demand.
                   CAST(SUM(CASE WHEN a.manual_adj > 0 THEN dem.demand ELSE 0 END)
                        AS DOUBLE)                   AS demand_through_hc,
                   SUM(dem.demand * a.manual_adj)    AS demand_x_manual,
                   MEDIAN(a.manual_adj)              AS median_manual,
                   -- `hc` is the IEDB's operators-per-station figure. Stable per
                   -- station (2,455 of 2,713 have a single value), so it is the
                   -- HAVE for DL sizing — real data, unlike the eq_avail default.
                   MEDIAN(a.hc)                      AS hc,
                   MEDIAN(a.fpy)                     AS fpy,
                   SUM(CASE WHEN a.ct_source = 'MOST' THEN 1 ELSE 0 END) AS src_most,
                   SUM(CASE WHEN a.ct_source = 'SW'   THEN 1 ELSE 0 END) AS src_sw,
                   SUM(CASE WHEN a.ct_source = 'Est'  THEN 1 ELSE 0 END) AS src_est
            FROM asm_ct a JOIN dem USING (asm)
            GROUP BY 1, 2
            HAVING SUM(dem.demand) > 0
            ORDER BY 1, 2
        """, [workcell]).df()
    finally:
        con.close()


# The CT expression each resource mode charges. Equipment uses the full adjusted
# cycle time; people (DL sizing) charge operator time only — machine time is not DL.
_CT_EXPR = {
    "equipment": "ct_adj",
    "people":    "(COALESCE(imt,0) + COALESCE(hand,0)) * COALESCE(sampling,100) / 100.0",
}


def _assembly_rows(workcell: str, sub_workcenter: str, process: str,
                   limit: int = 200, month: str | None = None,
                   resource: str = "equipment") -> pd.DataFrame:
    """Per-assembly evidence behind one process — the drill-down / math trail."""
    month = _resolve_month(month)
    ct_expr = _CT_EXPR[_check_resource(resource)]
    con = _con()
    try:
        return con.execute(f"""
            WITH dem AS ({_dem_sql(month)}),
            ct AS (
                SELECT {_NORM.format(col='assembly')} AS asm,
                       assembly, revision, mach, imt, hand, cap, sampling,
                       CASE WHEN type = 'Estimate'   THEN 'Est'
                            WHEN type = 'MOST'       THEN 'MOST'
                            WHEN type = 'Stop Watch' THEN 'SW'
                            ELSE 'Est' END AS ct_source,
                       updated_on,
                       cycle_time_per_process AS ct_adj
                FROM read_parquet('{CT_RAW.as_posix()}')
                WHERE workcell = ? AND sub_workcenter = ? AND process = ?
                  AND cycle_time_per_process > 0
            )
            SELECT ct.assembly, ANY_VALUE(ct.revision) AS revision,
                   ANY_VALUE(dem.demand)  AS demand,
                   MEDIAN(ct.ct_adj)      AS ct_adj,
                   MEDIAN({ct_expr})      AS ct_charged,
                   MEDIAN(ct.mach)        AS mach,
                   MEDIAN(ct.imt)         AS imt,
                   MEDIAN(ct.hand)        AS hand,
                   MEDIAN(ct.cap)         AS cap,
                   MEDIAN(ct.sampling)    AS sampling,
                   MODE(ct.ct_source)     AS ct_source,
                   MAX(ct.updated_on)     AS updated_on,
                   ANY_VALUE(dem.demand) * MEDIAN({ct_expr}) AS load
            FROM ct JOIN dem USING (asm)
            GROUP BY ct.assembly
            HAVING MEDIAN({ct_expr}) > 0
            ORDER BY load DESC
            LIMIT {int(limit)}
        """, [workcell, sub_workcenter, process]).df()
    finally:
        con.close()


def _assembly_totals(workcell: str, sub_workcenter: str, process: str,
                     month: str | None = None, resource: str = "equipment") -> dict:
    """
    Step totals over ALL assemblies, not just the top-N returned for display.

    _assembly_rows caps its result, so deriving the headline numbers from those
    rows would understate demand-through and skew load shares on big steps
    (KEYSIGHT/LAM/THERMO all exceed the cap).
    """
    month = _resolve_month(month)
    ct_expr = _CT_EXPR[_check_resource(resource)]
    con = _con()
    try:
        row = con.execute(f"""
            WITH dem AS ({_dem_sql(month)}),
            src AS (
                SELECT {_NORM.format(col='assembly')} AS asm, assembly,
                       CASE WHEN type = 'Estimate' THEN 1 ELSE 0 END AS is_est,
                       imt, hand, sampling,
                       cycle_time_per_process AS ct_adj
                FROM read_parquet('{CT_RAW.as_posix()}')
                WHERE workcell = ? AND sub_workcenter = ? AND process = ?
                  AND cycle_time_per_process > 0
            ),
            -- Second layer so _CT_EXPR can reference the ct_adj alias.
            ct AS (
                SELECT asm, is_est, {ct_expr} AS ct_adj FROM src
            ),
            asm_ct AS (
                SELECT asm, MEDIAN(ct_adj) AS ct_adj, MAX(is_est) AS is_est
                FROM ct GROUP BY asm HAVING MEDIAN(ct_adj) > 0
            )
            SELECT COUNT(*)                                            AS n,
                   CAST(SUM(dem.demand) AS DOUBLE)                     AS demand_thru,
                   SUM(dem.demand * a.ct_adj)                          AS total_load,
                   SUM(a.is_est)                                       AS est_count,
                   SUM(CASE WHEN a.is_est = 1 THEN dem.demand * a.ct_adj ELSE 0 END) AS est_load
            FROM asm_ct a JOIN dem USING (asm)
        """, [workcell, sub_workcenter, process]).fetchone()
    finally:
        con.close()

    return {
        "n": int(row[0] or 0),
        "demand_thru": _f(row[1]),
        "total_load": _f(row[2]),
        "est_count": int(row[3] or 0),
        "est_load": _f(row[4]),
    }


def _f(v, default=0.0) -> float:
    """NaN/None-safe float."""
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


RESOURCES = ("equipment", "people")


def _check_resource(resource: str | None) -> str:
    r = (resource or "equipment").lower()
    if r not in RESOURCES:
        raise ValueError(f"resource must be one of {RESOURCES}, got {resource!r}")
    return r


def build_capacity(workcell: str, month: str | None = None,
                   resource: str = "equipment", **overrides) -> dict:
    """
    Full forward capacity computation for one workcell, for ONE calendar month.

    Two resource modes — same steps, same demand, same takt, different numerator
    and different "have" (Training Package 6.0 slides 14-16, and SMT!C40
    "Resources NEEDED (DLs per Shift or Equipment)"):

      equipment  CT = mach/cap + imt + hand   ÷ Takt ÷ FPY ÷ Eff   vs  eq_avail
      people     CT = imt + hand              ÷ Takt ÷ Eff         vs  hc

    FPY is dropped for people: slide 16 says "FPY factors only for DL Required
    for related to Machine / Equipment only".

    Returns the nested structure the PPQT frontend renders.
    """
    month = _resolve_month(month)
    resource = _check_resource(resource)
    is_people = resource == "people"
    p = {**DEFAULTS, **{k: v for k, v in overrides.items() if v is not None}}
    avail = available_seconds(p["shift_hours"], p["working_days"],
                              p["co_qty"], p["co_time"])
    eff = p["efficiency"] / 100.0
    eq_avail = max(1, int(p["eq_avail"]))

    rows = _process_rows(workcell, month)
    if rows.empty:
        return {
            "workcell": workcell, "month": month, "resource": resource,
            "available_seconds": avail, "params": p,
            "total_demand": 0, "sub_workcenters": [], "warnings":
                [f"No overlapping cycle-time and planner-demand data for this workcell in {month}."],
        }

    sub_wcs: dict[str, dict] = {}
    for r in rows.itertuples():
        # People mode counts only assemblies needing an operator at this step, so
        # a fully automated station drops out instead of reporting 0 people short.
        demand = _f(r.demand_through_hc if is_people else r.demand_through)
        if demand <= 0:
            continue

        # §4.2 weighted cycle time — demand-weighted mean of adjusted CT
        wct = _f(r.demand_x_manual if is_people else r.demand_x_ct) / demand
        if wct <= 0:
            continue
        # A per-unit CT longer than an hour is a batch/dwell time, not a serial
        # station op — flagged so it stays out of the shortfall rollups below.
        dwell = wct > DWELL_CT_CEILING_SEC
        # §4.3 takt — seconds of capacity per unit of demand
        takt = avail / demand
        fpy = _f(r.fpy, 100.0) / 100.0
        fpy = fpy if 0 < fpy <= 1 else 1.0

        # HAVE: machines from the request param, people from the IEDB hc column.
        have = max(1, int(round(_f(r.hc, 1.0)))) if is_people else eq_avail

        # FPY applies to equipment only (slide 16).
        denom = takt * eff if is_people else takt * fpy * eff
        raw_need = (wct / denom) if denom > 0 else 0.0
        # Always ROUNDUP. NB the Excel does NOT: DASH!E33 is
        #   =IF(E34<>0, ROUNDUP(raw,0), ROUND(raw,0))
        # where row 34 "Round Up" is a manual per-process IE judgment flag. We
        # have no source for that flag, and rounding a fractional machine down
        # silently assumes shared equipment — so we always round up.
        need = int(math.ceil(raw_need - 1e-9)) if raw_need > 0 else 0
        util = (raw_need / have) * 100.0

        swc_name = str(r.sub_workcenter)
        swc = sub_wcs.setdefault(swc_name, {
            "id": _slug(swc_name), "name": swc_name, "area": _area(swc_name),
            "shiftHours": p["shift_hours"], "workingDays": p["working_days"],
            "changeoverQty": p["co_qty"], "changeoverTime": p["co_time"],
            "efficiency": p["efficiency"],
            "fpy": round((1.0 if is_people else fpy) * 100, 2),
            "processes": [],
        })
        counts = {
            "MOST": int(r.src_most or 0),
            "SW":   int(r.src_sw or 0),
            "Est":  int(r.src_est or 0),
        }
        swc["processes"].append({
            "id": f"{swc['id']}__{_slug(str(r.process))}",
            "name": str(r.process),
            "subWorkcenterId": swc["id"],
            "area": swc["area"],
            "demandThrough": int(round(demand)),
            "assemblyCount": int(r.assembly_count),
            "ctSourceCounts": counts,
            "primaryCtSource": max(counts, key=lambda k: counts[k]),
            "wct": round(wct, 2),
            "takt": round(takt, 2),
            "medianCt": round(_f(r.median_manual if is_people else r.median_ct), 2),
            "fpy": round((1.0 if is_people else fpy) * 100, 2),
            "eqAvail": have,
            "rawNeed": round(raw_need, 4),
            "resNeeded": need,
            "gap": need - eq_avail,
            "util": round(util, 1),
            "dwell": dwell,
        })

    out = []
    for swc in sub_wcs.values():
        procs = sorted(swc["processes"], key=lambda x: -x["util"])
        for i, proc in enumerate(procs, start=1):
            proc["sequence"] = i
        swc["processes"] = procs
        # Rollups size only the serial steps — dwell/parallel steps (burn-in,
        # ESS, test soak) can't be sized by the serial formula, so counting them
        # as "short" would report hundreds of missing operators/machines.
        sized = [x for x in procs if not x["dwell"]]
        swc["totalDemand"] = max((x["demandThrough"] for x in procs), default=0)
        swc["processCount"] = len(procs)
        swc["dwellCount"] = len(procs) - len(sized)
        swc["bottlenecks"] = sum(1 for x in sized if x["util"] > 100)
        swc["avgUtil"] = round(sum(x["util"] for x in sized) / len(sized), 1) if sized else 0.0
        swc["machinesShort"] = sum(max(0, x["gap"]) for x in sized)
        out.append(swc)

    out.sort(key=lambda s: -s["avgUtil"])
    all_procs = [x for s in out for x in s["processes"]]
    sized_procs = [x for x in all_procs if not x["dwell"]]

    return {
        "workcell": workcell,
        "month": month,
        "resource": resource,
        "available_seconds": avail,
        "params": p,
        "total_demand": max((s["totalDemand"] for s in out), default=0),
        "process_count": len(all_procs),
        "dwell_count": len(all_procs) - len(sized_procs),
        "bottlenecks": sum(1 for x in sized_procs if x["util"] > 100),
        "machines_short": sum(max(0, x["gap"]) for x in sized_procs),
        "avg_util": round(sum(x["util"] for x in sized_procs) / len(sized_procs), 1) if sized_procs else 0.0,
        "sub_workcenters": out,
        "warnings": [],
    }


@lru_cache(maxsize=32)
def build_overview(shift_hours: float, working_days: float, co_qty: float,
                   co_time: float, efficiency: float, eq_avail: int,
                   month: str, resource: str = "equipment") -> tuple:
    """
    Capacity rollup for every workcell — powers the league table.

    ~3s cold for all 16 workcells, so it is cached on the parameter tuple.
    Args are positional scalars (not a dict) purely so lru_cache can key them.
    Cache clears on process restart; the underlying marts are rebuilt by a
    separate pipeline, so staleness is bounded by deploy cadence.
    ponytail: lru_cache, swap for a TTL if the marts start refreshing in-process.
    """
    month = _resolve_month(month)
    resource = _check_resource(resource)
    out = []
    for wc in list_workcells(month).itertuples():
        cap = build_capacity(
            wc.workcell, month=month, resource=resource,
            shift_hours=shift_hours, working_days=working_days,
            co_qty=co_qty, co_time=co_time, efficiency=efficiency, eq_avail=eq_avail,
        )
        procs = [p for s in cap["sub_workcenters"] for p in s["processes"]]
        # Dwell/parallel steps aren't serially sized — keep them out of the verdict.
        sized = [p for p in procs if not p["dwell"]]
        steps_short = sum(1 for p in sized if p["gap"] > 0)
        steps_tight = sum(1 for p in sized if p["gap"] <= 0 and p["util"] >= 90)
        out.append({
            "workcell": wc.workcell,
            "resource": resource,
            "totalDemand": cap["total_demand"],
            "assemblies": int(wc.assemblies),
            "lines": len(cap["sub_workcenters"]),
            "stepsTotal": len(procs),
            "stepsShort": steps_short,
            "stepsTight": steps_tight,
            "machinesShort": cap["machines_short"],
            "avgUtil": cap["avg_util"],
            "verdict": "short" if steps_short else ("tight" if steps_tight else "ok"),
        })
    out.sort(key=lambda r: (-r["machinesShort"], -r["totalDemand"]))
    return tuple(out)


def line_matrix(workcell: str, sub_workcenter: str, top: int = 20,
                month: str | None = None, resource: str = "equipment") -> dict:
    """
    Excel DASH "TOP Demand" block: top-N assemblies by demand × their adjusted
    CT at every process on this line. One query — the alternative is one
    /evidence round trip per process.
    """
    _require(CT_RAW, "cycle time")
    _require(PLANNER_DEMAND, "planner demand")
    month = _resolve_month(month)
    ct_expr = _CT_EXPR[_check_resource(resource)]

    con = _con()
    try:
        df = con.execute(f"""
            WITH dem AS ({_dem_sql(month)}),
            src AS (
                SELECT {_NORM.format(col='assembly')} AS asm,
                       assembly, revision, process, imt, hand, sampling,
                       cycle_time_per_process AS ct_adj
                FROM read_parquet('{CT_RAW.as_posix()}')
                WHERE workcell = ? AND sub_workcenter = ?
                  AND cycle_time_per_process > 0
            ),
            ct AS (
                SELECT asm, assembly, revision, process, {ct_expr} AS ct_adj FROM src
            ),
            asm_ct AS (
                SELECT asm, ANY_VALUE(assembly) AS assembly,
                       ANY_VALUE(revision) AS revision, process,
                       MEDIAN(ct_adj) AS ct_adj
                FROM ct GROUP BY asm, process HAVING MEDIAN(ct_adj) > 0
            )
            SELECT a.assembly, a.revision, a.process, a.ct_adj, dem.demand
            FROM asm_ct a JOIN dem USING (asm)
        """, [workcell, sub_workcenter]).df()
    finally:
        con.close()

    if df.empty:
        return {"workcell": workcell, "subWorkcenter": sub_workcenter,
                "month": month, "resource": resource, "count": 0, "assemblies": []}

    # Rank assemblies by demand, keep the top N, then pivot CT by process.
    ranked = (df.groupby("assembly", as_index=False)
                .agg(demand=("demand", "max"), revision=("revision", "first"))
                .sort_values("demand", ascending=False)
                .head(int(top)))
    keep = set(ranked["assembly"])
    cts: dict[str, dict[str, float]] = {}
    for r in df.itertuples():
        if r.assembly in keep:
            cts.setdefault(str(r.assembly), {})[str(r.process)] = round(_f(r.ct_adj), 2)

    return {
        "workcell": workcell, "subWorkcenter": sub_workcenter, "month": month,
        "resource": resource, "count": len(ranked),
        "assemblies": [
            {
                "assembly": str(r.assembly),
                "revision": str(r.revision or ""),
                "demand": int(round(_f(r.demand))),
                "ct": cts.get(str(r.assembly), {}),
            }
            for r in ranked.itertuples()
        ],
    }


def process_evidence(workcell: str, sub_workcenter: str, process: str,
                     month: str | None = None, resource: str = "equipment") -> dict:
    """Assembly-level breakdown behind one process cell."""
    month = _resolve_month(month)
    resource = _check_resource(resource)
    is_people = resource == "people"
    LIMIT = 200
    df = _assembly_rows(workcell, sub_workcenter, process, LIMIT, month, resource)
    # Headline numbers come from the uncapped aggregate, the rows from the capped
    # query — otherwise a step with >LIMIT assemblies reports too little demand.
    totals = _assembly_totals(workcell, sub_workcenter, process, month, resource)
    total_load = totals["total_load"]
    items = []
    for r in df.itertuples():
        load = _f(r.load)
        demand = int(round(_f(r.demand)))
        source = str(r.ct_source or "Est")

        # §4.1 split — the drawer renders Mach/IMT/Hand as a stacked bar.
        cap = _f(r.cap, 1.0) or 1.0
        s = _f(r.sampling, 100.0) / 100.0
        # ct_charged is already the resource-appropriate CT (see _CT_EXPR).
        ct_shown = _f(r.ct_charged)
        # updated_on is a VARCHAR ISO string ("2025-10-07T15:16:00Z"), not a datetime.
        study = str(r.updated_on)[:10] if r.updated_on else None
        items.append({
            "assembly": str(r.assembly), "revision": str(r.revision or ""),
            "demand": demand,
            "ctAdj": round(ct_shown, 2),
            "mach": round(_f(r.mach), 2), "imt": round(_f(r.imt), 2),
            "hand": round(_f(r.hand), 2), "cap": round(cap, 2),
            "sampling": round(_f(r.sampling, 100.0), 1),
            "machAdj": round(_f(r.mach) / cap * s, 2),
            "imtAdj":  round(_f(r.imt) * s, 2),
            "handAdj": round(_f(r.hand) * s, 2),
            "ctSource": source,
            "studyDate": study,
            "load": round(load, 1),
            "loadShare": round(load / total_load * 100, 1) if total_load > 0 else 0.0,
        })
    return {
        "workcell": workcell, "subWorkcenter": sub_workcenter, "process": process,
        "month": month, "resource": resource,
        "totalLoad": round(total_load, 1),
        "count": len(items),
        "assemblyCount": totals["n"],
        "truncated": totals["n"] > len(items),
        "demandThruStep": int(round(totals["demand_thru"])),
        "estCount": totals["est_count"],
        "estLoadShare": round(totals["est_load"] / total_load * 100, 1) if total_load > 0 else 0.0,
        "assemblies": items,
    }


# ─── Self-check ───────────────────────────────────────────────────────────────
def _selfcheck() -> None:
    """Smallest thing that fails if the forward chain breaks. Run: python -m modules.ppqt.capacity"""
    # §4.3: (21h×60 − 4×20)min × 26d × 60 = 1,840,800 s
    assert available_seconds(21, 26, 4, 20) == 1_840_800.0, available_seconds(21, 26, 4, 20)
    assert available_seconds(0, 26, 4, 20) == 0.0          # never negative

    # §4.4 end-to-end, hand-checked:
    #   avail 1,840,800 s, demand 2,000 → takt 920.4 s
    #   wct 1,200 s, fpy 100%, eff 85% → raw = 1200/(920.4×1×0.85) = 1.5337 → need 2
    takt = 1_840_800.0 / 2_000
    raw = 1_200 / (takt * 1.0 * 0.85)
    assert abs(raw - 1.5337) < 1e-3, raw
    assert math.ceil(raw - 1e-9) == 2

    # ROUNDUP must not trip on float fuzz: exactly 2.0 stays 2, not 3
    assert math.ceil(2.0 - 1e-9) == 2

    # Dwell guard: a 12h/unit hand time is a batch/dwell value, not a serial op.
    assert 44_565.0 > DWELL_CT_CEILING_SEC       # LAM Solder 3 → flagged dwell
    assert 30.0 <= DWELL_CT_CEILING_SEC          # a real SMT CT stays serial

    # Monthly defaults match the Wabtec workbook exactly (DASH!E30):
    #   (21*60 - 4*20) * (26*60) / 5506 = 334.33 s  ← the sheet's "Takt Time (Min)",
    # which is actually seconds despite the label.
    assert abs(available_seconds(21, 26, 4, 20) / 5506 - 334.326) < 1e-2
    assert DEFAULTS["working_days"] == 26

    # Month validation guards the SQL interpolation, and ALWAYS yields a
    # concrete month — never a query that spans every period.
    assert _resolve_month("2026-07") == "2026-07"
    assert _resolve_month(None) == default_month() and _MONTH_RE.match(_resolve_month(None))
    assert _resolve_month("") == default_month()
    for bad in ("2026-13", "26-07", "2026-7", "2026-00", "2026-07'; DROP TABLE x--"):
        try:
            _resolve_month(bad)
            raise AssertionError(f"accepted bad month {bad!r}")
        except ValueError:
            pass
    # Every demand query must be month-scoped.
    assert "WHERE strftime" in _dem_sql(_resolve_month(None))

    # Resource validation
    assert _check_resource(None) == "equipment" and _check_resource("PEOPLE") == "people"
    for bad in ("machines", "dl", "hc"):
        try:
            _check_resource(bad); raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass
    # People mode must charge operator time only — never machine time.
    assert "mach" not in _CT_EXPR["people"]
    assert "imt" in _CT_EXPR["people"] and "hand" in _CT_EXPR["people"]

    # _area classification
    assert _area("WAB SMT P1A-1 B5a") == "SMT"
    assert _area("WAB TH P1A-1 B5a") == "TH"
    assert _area("WAB BE CC P1B-2") == "BE"

    # NaN-safe float
    assert _f(float("nan")) == 0.0 and _f(None, 100.0) == 100.0 and _f("3.5") == 3.5
    print("capacity.py self-check OK")


if __name__ == "__main__":
    _selfcheck()
