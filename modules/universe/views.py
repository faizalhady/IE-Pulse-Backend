"""
modules/universe/views.py
─────────────────────────
The semantic layer — the tables a person or a model actually reads.

Every column carries a comment in Jabil's words, because the names alone cannot
say that workcell = customer, that units are boards counted once, or that "which
plant" is two facts. This is what v1's facts.py was, rebuilt on the universe:
the traps are pre-solved as columns, and the meaning travels with the schema
(`select column_name, comment from duckdb_columns()`).

Nothing above this layer reads the parquet directly.

    from modules.universe import views
    con = views.connect()
    con.execute("select * from v_workcell where status = 'active'").df()
"""

from __future__ import annotations

import duckdb

from modules.universe.config import UNIVERSE_MART

# view name -> (sql, {column: comment})
VIEWS: dict[str, tuple[str, dict[str, str]]] = {
    "v_workcell": (
        """
        select w.workcell_id, w.name as workcell, w.entity_type, w.status,
               w.plant_physical, w.plant_governing, w.region, w.division,
               w.mes_customer_id_primary as mes_customer_id,
               p.name as parent_proposed, w.confidence
        from dim_workcell w
        left join dim_workcell p on p.workcell_id = w.parent_id_proposed
        """,
        {
            "workcell_id": "The one id for a workcell. Every other table joins on this, never on a name.",
            "workcell": "Workcell = CUSTOMER — the customer-dedicated production organisation (e.g. KEYSIGHT, WABTEC). Not a station, not a cell on a line.",
            "entity_type": "customer = a normal customer workcell · shared_line = an internal line many customers use (AOP runs SMT for others) · support = has people, builds nothing (WAREHOUSE) · unknown = the row scans land on when no customer resolves.",
            "status": "active or inactive in the August 2026 registry. 'How many workcells' has several true answers — say which filter you used.",
            "plant_physical": "Where the workcell physically sits: P1, P2 or BK (Batu Kawan). MICRON SIG, LAMGB, LAMMEC sit in BK.",
            "plant_governing": "Which plant supervises it. Differs from plant_physical for MICRON SIG, LAMGB, LAMMEC (BK floor, Plant 1 supervision). 'In P1' is two questions — ask which.",
            "region": "Penang Island (P1 + P2) or Batu Kawan, following the physical plant.",
            "division": "MES division text, an attribute of the workcell, not a level above it.",
            "mes_customer_id": "The primary MES Customer_ID. Some workcells hold two (KEYSIGHT is 7 and 114) — the full set is in workcell_alias.",
            "parent_proposed": "The parent workcell the August registry PROPOSED (KEYSIGHT for K_CTEC …). Families are not yet a confirmed fact; do not roll up on this without saying so.",
            "confidence": "extracted = seen in source data · guess = inferred by the August registry build · n/a = synthetic row.",
        },
    ),
    "v_units_out_daily": (
        """
        select w.name as workcell, u.workcell_id, m.part_number as assembly, u.model_id,
               u.date, u.shift, c.iso_year, c.iso_week, c.fiscal_year, c.fiscal_quarter,
               count(*) as units_out,
               any_value(u.terminal_step) as terminal_step, any_value(u.learned) as terminal_learned
        from fact_unit_out u
        join dim_workcell w on w.workcell_id = u.workcell_id
        left join dim_model m on m.model_id = u.model_id
        join dim_calendar c on c.date = u.date
        group by all
        """,
        {
            "workcell": "Workcell = customer the board was built for. Resolved through the registry, never a raw MES string.",
            "workcell_id": "Join key to v_workcell.",
            "assembly": "The part number = model = assembly — one thing, three words. A model is (workcell, assembly) together.",
            "model_id": "Join key to dim_model (assembly × revision lives beneath it).",
            "date": "Local (Asia/Singapore) date of the completing scan. MES timestamps were UTC; converted first.",
            "shift": "2 = 07:00–19:00, 3 = 19:00–07:00. Production runs only on these two; a scan before 07:00 belongs to the previous date's shift 3 — see shift_date in fact_scan.",
            "iso_year": "ISO year of the date — it LAGS the calendar year at year end (1 Jan 2027 is still 2026-W53).",
            "iso_week": "ISO week 1–53. 2026 has a week 53.",
            "fiscal_year": "Jabil fiscal year — starts in SEPTEMBER. Sep 2026 is Q1 of FY2027.",
            "fiscal_quarter": "Q1 Sep–Nov · Q2 Dec–Feb · Q3 Mar–May · Q4 Jun–Aug.",
            "units_out": "Boards completed, each counted ONCE, at the model's terminal step. Not scan rows: rework loops re-scan a step and would double count.",
            "terminal_step": "The step this model's boards finish at — learned from the boards themselves (PACKOUT for most; some routes end earlier). LINK is a logistics scan after completion and is never the terminal step unless nothing else was seen.",
            "terminal_learned": "true = learned from >= 5 boards with a >= 50% majority · false = too little history, PACKOUT assumed.",
        },
    ),
    "v_ole_weekly": (
        """
        select workcell, workcell_id, iso_year, iso_week, scan_days,
               units, units_missing_smh, earned_smh, paid_hours, ole_universe,
               ole_module, delta_pts, reason
        from ole_reconciliation
        """,
        {
            "workcell": "Workcell = customer.",
            "workcell_id": "Join key to v_workcell.",
            "iso_year": "ISO year of the week.",
            "iso_week": "ISO week. OLE is reported by ISO week (Mon–Sun).",
            "scan_days": "Days of this week the scan pull covers (7 = full week). The August pull runs 9 Jul → 8 Aug 2026.",
            "units": "Boards completed this week, once each (see v_units_out_daily.units_out).",
            "units_missing_smh": "Of those units, how many have no SMH standard — they earn zero hours here. The OLE module ESTIMATES a standard for these; the universe does not.",
            "earned_smh": "Σ units × SMH per unit. SMH = standard man-hours: the labour a unit is worth at standard.",
            "paid_hours": "Σ paid direct-labour hours for the workcell this week (payroll), all shifts.",
            "ole_universe": "OLE = earned_smh ÷ paid_hours × 100. Overall Labour Effectiveness — the labour twin of OEE. High is good. Computed from universe tables only.",
            "ole_module": "The OLE module's own weekly number for the same workcell-week (its marts, its definitions) — for comparison only.",
            "delta_pts": "ole_universe − ole_module, in percentage points.",
            "reason": "Why they differ, computed from the inputs: unit definition, SMH coverage, paid-hours scope, partial week. Empty when within 2 points.",
        },
    ),
}


def connect() -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB holding every universe table as a view plus the semantic
    views above, column comments included. Close it when done."""
    con = duckdb.connect()
    for name, path in UNIVERSE_MART.items():
        if path.exists():
            con.execute(f"create view {name} as select * from read_parquet('{path.as_posix()}')")
    for view, (sql, comments) in VIEWS.items():
        con.execute(f"create view {view} as {sql}")
        for col, text in comments.items():
            con.execute(f"comment on column {view}.{col} is '{text.replace(chr(39), chr(39) * 2)}'")
    return con


def describe(view: str) -> list[tuple[str, str, str]]:
    """(column, type, comment) — what a model should read before writing SQL."""
    con = connect()
    try:
        return con.execute(
            "select column_name, data_type, comment from duckdb_columns() where table_name = ? order by column_index",
            [view]).fetchall()
    finally:
        con.close()


if __name__ == "__main__":
    for v in VIEWS:
        print(f"\n{v}")
        for col, typ, com in describe(v):
            print(f"  {col:20} {typ:10} {com}")
