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
    # ── Phase 2 ──
    "v_employee": (
        """
        select e.employee_id, e.payroll_no, e.name, e.scope, w.name as workcell, e.workcell_id,
               d.name as department, e.department_id, e.job_category, e.job_family, e.business_title,
               e.worker_type, e.employee_type, e.hire_date, e.manager_employee_id, e.link_status
        from dim_employee e
        left join dim_workcell w on w.workcell_id = e.workcell_id
        left join dim_department d on d.department_id = e.department_id
        """,
        {
            "employee_id": "HR employee number — the person's key.",
            "payroll_no": "The number payroll / eTMS uses for the same person; fact_paid_hours.employee_no joins here.",
            "name": "Name as HR has it.",
            "scope": "workcell = dedicated to one customer workcell · site = serves all workcells (a site IE engineer). Site scope is a real fact, not missing data.",
            "workcell": "The customer workcell this person is dedicated to, when scope = workcell. NULL for site scope.",
            "workcell_id": "Join key to v_workcell.",
            "department": "What the person does — IE, ME, TE, MFG, QA, Finance… (28 departments). Not who they do it for.",
            "department_id": "Join key to dim_department.",
            "job_category": "HR job category.",
            "job_family": "HR job family.",
            "business_title": "The title on the org chart.",
            "worker_type": "HR worker type (e.g. direct / indirect).",
            "employee_type": "HR employee type.",
            "hire_date": "Date joined Jabil.",
            "manager_employee_id": "Solid-line manager (reports-to). Dotted-line governance is recorded nowhere — case 33.",
            "link_status": "matched = our workcell mapping found the person's workcell · unmatched = a mapping gap on OUR side, not a fact about the person.",
        },
    ),
    "v_process": (
        """
        select p.process_id, p.alias, p.process_kind, p.name, p.work_kind, p.workcenter, p.workcenter_type,
               p.mes_steps, p.models, p.lines, p.customers, p.avg_sec
        from dim_process p
        """,
        {
            "process_id": "The key of one process at the ALIAS level — the thing a cycle time sits on.",
            "alias": "IEDB alias — the specific variant, e.g. 'HLA (CPU) 1'. This is the identity (case 16).",
            "process_kind": "IEDB process — the kind of work above the alias, e.g. 'Assembly 1' (266 kinds).",
            "name": "Display name.",
            "work_kind": "manual · machine · mixed — who does the work.",
            "workcenter": "SMT · TH · BE — the process STAGE (not geography; 'area' means geography elsewhere).",
            "workcenter_type": "Finer stage label (HLA is a kind of BE).",
            "mes_steps": "The MES step names this process scans at, when mapped.",
            "models": "How many models use this process (registry count).",
            "lines": "How many lines run it.",
            "customers": "How many workcells use it.",
            "avg_sec": "Average study cycle time across uses, seconds — orientation only; the studies are in v_cycle_time.",
        },
    ),
    "v_cycle_time": (
        """
        select w.name as workcell, s.workcell_id, m.part_number as assembly, s.model_id, s.revision_raw as revision,
               s.line_id, s.step_order, s.process_alias_raw as alias, s.process_raw as process_kind,
               s.workcenter, s.cycle_time_sec, s.mach_sec, s.imt_sec, s.hand_sec, s.headcount, s.parallel_cap,
               s.fpy, s.ct_status, s.updated_on, s.study_id
        from fact_cycle_time_study s
        join dim_workcell w on w.workcell_id = s.workcell_id
        left join dim_model m on m.model_id = s.model_id
        """,
        {
            "workcell": "Workcell = customer.",
            "workcell_id": "Join key to v_workcell.",
            "assembly": "Part number = model = assembly.",
            "model_id": "Join key to dim_model.",
            "revision": "The revision this study is for. Standards are per revision; a new revision does NOT automatically inherit (open question).",
            "line_id": "The line (sub-workcenter) the route runs on — IEDB only.",
            "step_order": "Position of the step on the route.",
            "alias": "The process alias — the identity the time sits on.",
            "process_kind": "The kind of work (IEDB process).",
            "workcenter": "SMT · TH · BE stage.",
            "cycle_time_sec": "The STANDARD: stopwatch work content per unit, seconds, from an IE time study. Never an elapsed time — that is fact_cycle_time_measured.",
            "mach_sec": "Machine time component.",
            "imt_sec": "Operator-at-machine component.",
            "hand_sec": "Pure hand time component.",
            "headcount": "Operators at the step.",
            "parallel_cap": "Parallel capacity within ONE study row (panel-up). NOT machines available.",
            "fpy": "First-pass yield assumed by the study.",
            "ct_status": "measured = a study with a time · missing = the step exists in IEDB with no time. Absence is a value (case 41).",
            "updated_on": "When IEDB last touched the row.",
            "study_id": "The IEDB row id — studies are append-only events.",
        },
    ),
    "v_route": (
        """
        select w.name as workcell, m.workcell_id, mo.part_number as assembly, r.model_id, r.line_id,
               r.step_order, r.step_group, r.process_alias as alias, r.process as process_kind, r.process_id,
               r.workcenter, r.station, r.cycle_time_sec, r.headcount, r.is_operator_step
        from fact_route r
        left join dim_model mo on mo.model_id = r.model_id
        left join dim_model m on m.model_id = r.model_id
        left join dim_workcell w on w.workcell_id = m.workcell_id
        """,
        {
            "workcell": "Workcell = customer.",
            "workcell_id": "Join key to v_workcell.",
            "assembly": "Part number = model = assembly.",
            "model_id": "Join key to dim_model.",
            "line_id": "The line this route variant runs on.",
            "step_order": "Order of the step on the route — sort on this for 'end to end'. It restarts per line_id: a model with two lines has two routes; group or filter by line_id first.",
            "step_group": "Steps that belong together (a buffer point) when known.",
            "alias": "Process alias at this step.",
            "process_kind": "Kind of work at this step.",
            "process_id": "Join key to v_process. NULL = the step is not mapped to an IEDB process yet (cases 23–25).",
            "workcenter": "SMT · TH · BE stage.",
            "station": "The station / scan point name where this step is done.",
            "cycle_time_sec": "Standard time at this step, seconds (from the study).",
            "headcount": "Operators at the step.",
            "is_operator_step": "true = an operator does it; false = machine.",
        },
    ),
    "v_demand": (
        """
        select w.name as workcell, d.workcell_id, m.part_number as assembly, d.model_id,
               d.period_start, d.period_type, d.qty, d.source, d.as_of
        from fact_demand d
        join dim_workcell w on w.workcell_id = d.workcell_id
        left join dim_model m on m.model_id = d.model_id
        """,
        {
            "workcell": "Workcell = customer — resolved through the registry, never the planner's raw spelling.",
            "workcell_id": "Join key to v_workcell.",
            "assembly": "Part number = model = assembly — the key the planner and cycle time are joined on (case 18).",
            "model_id": "Join key to dim_model.",
            "period_start": "Start of the planning period (Monday for weeks).",
            "period_type": "week (13-week planner) or month.",
            "qty": "Units planned in the period.",
            "source": "Which planner sheet the number came from.",
            "as_of": "The date the planner snapshot was taken — demand changes every week; say which snapshot.",
        },
    ),
    "v_fpy_daily": (
        """
        select w.name as workcell, s.workcell_id, m.part_number as assembly, s.model_id, s.step, s.date,
               count(distinct s.wip_id) filter (where s.test_status = 'P') as boards_passed,
               count(distinct s.wip_id) as boards_tested,
               round(count(distinct s.wip_id) filter (where s.test_status = 'P') * 1.0 / count(distinct s.wip_id), 4) as fpy
        from fact_scan s
        join dim_workcell w on w.workcell_id = s.workcell_id
        left join dim_model m on m.model_id = s.model_id
        -- a real TEST step passes boards sometimes; an 'F' at SCRAP, BIRTH or RTC is a
        -- disposition, not a test result (trial 2 reported FPY = 0.00 at SCRAP)
        join (select workcell_id, step from fact_scan
              where test_loop = 1 and test_status = 'P' group by 1, 2) ts
          on ts.workcell_id = s.workcell_id and ts.step = s.step
        where s.test_loop = 1 and s.test_status in ('P', 'F')
          and s.step not ilike '%SCRAP%' and s.step not ilike '%RTC%' and s.step not ilike 'BIRTH%'
        group by all
        """,
        {
            "workcell": "Workcell = customer.",
            "workcell_id": "Join key to v_workcell.",
            "assembly": "Part number = model = assembly.",
            "model_id": "Join key to dim_model.",
            "step": "The MES test step (ICT, FVT …) where the result was recorded. Only steps that pass boards count as test steps — SCRAP, RTC and BIRTH are dispositions, never tests.",
            "date": "Local date of the test scan.",
            "boards_passed": "Distinct boards whose FIRST test at this step passed.",
            "boards_tested": "Distinct boards tested at this step, first loop only (test_loop = 1). Retests are excluded — FPY is first pass.",
            "fpy": "First-pass yield = boards_passed ÷ boards_tested. 'A' (aborted) results are excluded from both.",
        },
    ),
    "v_output_daily": (
        """
        select 'boards' as source, workcell, workcell_id, assembly, model_id, date, shift, units_out, iso_year, iso_week
        from v_units_out_daily
        union all
        select 'share', w.name, p.workcell_id, p.assembly_raw, p.model_id, p.date, p.shift, sum(p.qty), c.iso_year, c.iso_week
        from fact_production_share p
        join dim_workcell w on w.workcell_id = p.workcell_id
        join dim_calendar c on c.date = p.date
        group by all
        """,
        {
            "source": "boards = distinct boards at the model's terminal step, from MES scans (9 Jul → 8 Aug 2026) · share = quantities from the OLE production share (15 Mar → 3 Aug 2026). They count DIFFERENTLY — never add them; compare them.",
            "workcell": "Workcell = customer.",
            "workcell_id": "Join key to v_workcell.",
            "assembly": "Part number as each source spells it (boards: registry; share: the share file).",
            "model_id": "Join key to dim_model — NULL when the share's spelling did not resolve.",
            "date": "Local date.",
            "shift": "2 or 3 — production shifts.",
            "units_out": "Units for that source's definition (see source).",
            "iso_year": "ISO year.",
            "iso_week": "ISO week.",
        },
    ),
    "v_ole_daily": (
        """
        with units as (
          select u.workcell_id, u.model_id, u.shift_date as date, u.shift from fact_unit_out u
        ),
        smh as (select workcell_id, model_id, max(smh_per_unit) as smh_per_unit from dim_smh group by 1, 2),
        joined as (select un.*, s.smh_per_unit from units un
                   left join smh s on s.workcell_id = un.workcell_id and s.model_id = un.model_id),
        wc_avg as (select workcell_id, sum(smh_per_unit) / count(*) as avg_smh from joined where smh_per_unit is not null group by 1),
        earned as (
          select j.workcell_id, j.date, j.shift, count(*) as units,
                 count(*) filter (where j.smh_per_unit is null) as units_missing_smh,
                 sum(case when j.smh_per_unit is not null then j.smh_per_unit
                          when smh_policy() = 'estimate' then coalesce(a.avg_smh, 0) else 0 end) as earned_smh
          from joined j left join wc_avg a on a.workcell_id = j.workcell_id
          group by 1, 2, 3
        ),
        paid as (select workcell_id, date, shift, sum(paid_hours) as paid_hours from fact_paid_hours group by 1, 2, 3)
        select w.name as workcell, e.workcell_id, e.date, e.shift, e.units, e.units_missing_smh,
               round(e.earned_smh, 2) as earned_smh, round(p.paid_hours, 2) as paid_hours,
               case when p.paid_hours > 0 then round(e.earned_smh / p.paid_hours * 100, 2) end as ole,
               smh_policy() as smh_policy
        from earned e
        join paid p using (workcell_id, date, shift)
        join dim_workcell w on w.workcell_id = e.workcell_id
        """,
        {
            "workcell": "Workcell = customer.",
            "workcell_id": "Join key to v_workcell.",
            "date": "The shift's date (a scan before 07:00 belongs to the previous date's night shift).",
            "shift": "2 = 07:00–19:00, 3 = 19:00–07:00.",
            "units": "Boards completed this shift, once each, at the model's terminal step.",
            "units_missing_smh": "Of those, units with no SMH standard. What they earn depends on smh_policy.",
            "earned_smh": "Σ units × SMH per unit — standard man-hours earned. Under policy 'estimate', units without a standard earn the workcell's average.",
            "paid_hours": "Paid direct-labour hours for the workcell on this shift (payroll).",
            "ole": "OLE = earned ÷ paid × 100. The labour twin of OEE; high is good.",
            "smh_policy": "zero = units without SMH earn nothing and the gap is visible (universe default) · estimate = they earn the workcell average (what the OLE module does with OLE_SMH_FALLBACK=avg). Case 62.",
        },
    ),
}


def connect() -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB holding every universe table as a view plus the semantic
    views above, column comments included. Close it when done."""
    con = duckdb.connect()
    con.execute("set enable_progress_bar = false")
    from modules.universe.config import SMH_MISSING_POLICY
    con.create_function("smh_policy", lambda: SMH_MISSING_POLICY, [], str)
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


# --- Wave 4 (2026-08-23): places and things that were boxes on the atlas ---------
VIEWS.update({
    "v_headcount": (
        """
        select w.name as workcell, e.workcell_id, d.name as department, e.department_code, e.scope,
               e.job_category, e.worker_type, e.employee_type, count(*) as people
        from dim_employee e
        left join dim_workcell w on w.workcell_id = e.workcell_id
        left join dim_department d on d.department_id = e.department_id
        group by all
        """,
        {
            "workcell": "Workcell = CUSTOMER the people are dedicated to. NULL = site scope (serves all workcells) or unmapped.",
            "workcell_id": "Join key to v_workcell.",
            "department": "What the people do (IE, MFG, QA ...), not who they do it for.",
            "department_code": "Short department code.",
            "scope": "workcell = dedicated to one customer; site = serves the whole site.",
            "job_category": "HR job category (DL = direct labour, IL = indirect ...).",
            "worker_type": "HR worker type.",
            "employee_type": "HR employee type (Regular, Contract ...).",
            "people": "Headcount in this group, from the HR extract (as_of in v_employee). Agency workers are NOT here - they live in payroll only (case 64): use v_paid_hours_weekly for hours.",
        },
    ),
    "v_paid_hours_weekly": (
        """
        select w.name as workcell, p.workcell_id, cast(date_trunc('week', p.date) as date) as week_start,
               isoyear(p.date) as iso_year, week(p.date) as iso_week,
               count(distinct p.employee_no) as people, sum(p.paid_hours) as paid_hours,
               count(distinct p.employee_no) filter (where e.employee_id is null) as people_not_in_hr
        from fact_paid_hours p
        left join dim_workcell w on w.workcell_id = p.workcell_id
        left join dim_employee e on e.payroll_no = p.employee_no
        group by all
        """,
        {
            "workcell": "Workcell = CUSTOMER the hours were paid under. UNKNOWN holds 46% of all hours - 26 cost-centre codes nobody has mapped (case 72). Say so when it matters.",
            "workcell_id": "Join key to v_workcell.",
            "week_start": "Monday of the ISO week.",
            "iso_year": "ISO year.",
            "iso_week": "ISO week number.",
            "people": "Distinct payroll numbers paid that week - direct workers, agency included.",
            "paid_hours": "Sum of TPHDirect - every paid direct hour, productive or not. The OLE denominator.",
            "people_not_in_hr": "Payroll numbers HR does not know: agency / contract codes (case 64), or joiners newer than the HR extract.",
        },
    ),
    "v_department": (
        """
        select d.department_id, d.code, d.name, d.kind, p.name as parent, d.people, d.dl, d.il, d.other,
               d.workcells_covered, d.cost_centers, d.job_family_groups
        from dim_department d
        left join dim_department p on p.department_id = d.parent_id
        """,
        {
            "department_id": "Department key.",
            "code": "Short code (MFG, IE, QA ...).",
            "name": "Department name as HR has it.",
            "kind": "Kind of department where classified; NULL = not yet.",
            "parent": "Parent department, where known.",
            "people": "Headcount in the HR extract.",
            "dl": "Direct labour headcount.",
            "il": "Indirect labour headcount.",
            "other": "Other headcount.",
            "workcells_covered": "How many customer workcells the department serves - departments serve workcells in a matrix (case 33).",
            "cost_centers": "Cost centres seen for this department.",
            "job_family_groups": "HR job family groups in it.",
        },
    ),
    "v_scan_point": (
        """
        select w.name as workcell, s.workcell_id, s.mes_step, s.is_scan_point, s.parent_key, s.method,
               s.models, s.models_total, s.agreement, s.alternatives, s.review
        from dim_scan_point s
        left join dim_workcell w on w.workcell_id = s.workcell_id
        """,
        {
            "workcell": "Workcell = CUSTOMER whose route the step belongs to.",
            "workcell_id": "Join key to v_workcell.",
            "mes_step": "The MES step name as scanned (the station where a board is observed).",
            "is_scan_point": "true = boards are scanned here; false = a step that exists in the route but is not a scan point.",
            "parent_key": "For a non-scan step, the scan point downstream that observes it.",
            "method": "How the link was learned (e.g. next scan downstream).",
            "models": "Models where this link was seen.",
            "models_total": "Models with the step at all.",
            "agreement": "Share of models agreeing with the link (0-1). Below ~0.8 say it is uncertain.",
            "alternatives": "Other candidate scan points seen.",
            "review": "A note when a person should confirm.",
        },
    ),
    "v_bay": (
        """
        select b.bay_id, b.name as bay, b.naming_scheme, b.plant, b.area, b.building, b.level, b.bay_type, b.status,
               b.description, b.sources, b.merge_candidate_id, b.merge_confidence,
               b.scan_first_seen, b.scan_last_seen, b.scans, b.scan_workcells,
               (select string_agg(w.name, ', ' order by x.boards desc)
                from (select f.workcell_id, sum(f.boards) as boards from fact_bay_week f
                      where upper(trim(f.bay)) = upper(trim(b.name)) group by 1) x
                join dim_workcell w on w.workcell_id = x.workcell_id) as workcells_observed
        from dim_bay b
        """,
        {
            "bay_id": "Bay key. NOT comparable across naming schemes (case 9).",
            "bay": "The bay's name. Two schemes coexist: layout names (BAY 15A, OLFS) and MES names (BAY 105, HLA B2L). They are not reconciled - say which scheme you used.",
            "naming_scheme": "layout = the floor-plan / FSMS name; mes = the name MES scans carry (manufacturing_area).",
            "plant": "P1, P2 or BK. For MES bays, taken from the scans.",
            "area": "Geography inside the plant (P1A, P1B ...) - layout bays only.",
            "building": "Building letter - layout bays only.",
            "level": "Floor level - layout bays only.",
            "bay_type": "production; system (ALL BAY); special ...",
            "status": "Layout status: occupied, reserve, Active ...",
            "description": "Layout description where present.",
            "sources": "Where the row came from (mes_bay_layout, fsms, fact_scan ...).",
            "merge_candidate_id": "The bay in the OTHER scheme this one probably is - a candidate, not a fact.",
            "merge_confidence": "Confidence of that candidate (0-1).",
            "scan_first_seen": "First scan date in this bay (MES bays).",
            "scan_last_seen": "Last scan date (MES bays).",
            "scans": "Scan rows in this bay, 9 Jul to 22 Aug 2026.",
            "scan_workcells": "Distinct workcells scanned here.",
            "workcells_observed": "Workcells whose boards were scanned here, most boards first - the honest answer to 'who builds in this bay'.",
        },
    ),
    "v_bay_occupancy": (
        """
        select o.bay_id, o.bay, o.naming_scheme, w.name as workcell, o.workcell_id, o.evidence, o.source,
               o.units_90d, o.assemblies_90d, o.last_build, o.valid_from, o.valid_to
        from bay_occupancy o
        left join dim_workcell w on w.workcell_id = o.workcell_id
        """,
        {
            "bay_id": "Bay key (see v_bay).",
            "bay": "Bay name.",
            "naming_scheme": "layout or mes - the scheme the bay_id belongs to.",
            "workcell": "Workcell = CUSTOMER occupying the bay per this evidence.",
            "workcell_id": "Join key to v_workcell.",
            "evidence": "observed_production = boards were scanned there; configured_in_mes = MES maps the workcell to the bay; declared_on_layout = drawn on the floor plan / FSMS. Observed beats declared.",
            "source": "The dataset the evidence came from.",
            "units_90d": "Units observed there in the 90 days before the August registry (observed rows only).",
            "assemblies_90d": "Distinct assemblies in those 90 days.",
            "last_build": "Last build date observed.",
            "valid_from": "Start of the occupancy, when recorded.",
            "valid_to": "End of the occupancy; NULL = current.",
        },
    ),
    "v_bay_activity": (
        """
        select w.name as workcell, f.workcell_id, f.bay, f.plant, f.week_start,
               isoyear(f.week_start) as iso_year, week(f.week_start) as iso_week,
               f.boards, f.scans, f.first_scan, f.last_scan
        from fact_bay_week f
        left join dim_workcell w on w.workcell_id = f.workcell_id
        """,
        {
            "workcell": "Workcell = CUSTOMER whose boards were scanned.",
            "workcell_id": "Join key to v_workcell.",
            "bay": "MES bay name (manufacturing_area) - the MES scheme. 'Where does WABTEC build' is answered here, by week.",
            "plant": "P1, P2 or BK as the scan carried it.",
            "week_start": "Monday of the ISO week.",
            "iso_year": "ISO year.",
            "iso_week": "ISO week number.",
            "boards": "Distinct boards scanned in that bay that week (any step).",
            "scans": "Scan rows.",
            "first_scan": "First scan date that week.",
            "last_scan": "Last scan date that week.",
        },
    ),
    "v_line": (
        """
        select l.line_id, l.name as line, w.name as workcell, l.workcell_id, l.line_type, l.workcenter, l.workcenter_type,
               l.plant, l.building, l.sub_area, l.area, l.bay_code, l.cell, l.parsed, l.ct_rows, l.models, l.steps,
               l.total_ct_min, l.updated_on, l.review
        from dim_line l
        left join dim_workcell w on w.workcell_id = l.workcell_id
        """,
        {
            "line_id": "Line key.",
            "line": "The IEDB sub_workcenter name (e.g. KYS TH P1A-1 B15) - the engineering unit inside a bay. Only IEDB knows lines; MES has none.",
            "workcell": "Workcell = CUSTOMER the line belongs to.",
            "workcell_id": "Join key to v_workcell.",
            "line_type": "TH, SMT, BE ... as coded in the name.",
            "workcenter": "IEDB workcenter code.",
            "workcenter_type": "Workcenter type text.",
            "plant": "Plant parsed from the name.",
            "building": "Building parsed from the name.",
            "sub_area": "Sub-area parsed.",
            "area": "Area parsed (P1A, P1B ...).",
            "bay_code": "Layout bay code parsed from the name (BAY 15) - 147 of 230 lines parse (cases 26, 27).",
            "cell": "Cell, when named.",
            "parsed": "true = the name parsed to plant, building, bay.",
            "ct_rows": "Cycle-time study rows on this line.",
            "models": "Models studied on it.",
            "steps": "Process steps studied on it.",
            "total_ct_min": "Sum of standard minutes across its studies.",
            "updated_on": "Last IEDB update seen.",
            "review": "A note when a person should confirm.",
        },
    ),
    "v_asset": (
        """
        select a.asset_id, a.description, a.manufacturer, a.model, a.equip_category, a.is_smart_torque, a.torque_range,
               a.lifecycle, a.est1c_status, coalesce(w.name, a.workcell) as workcell, a.workcell_id,
               a.bay_raw, a.bay_area, a.location, a.room, a.pc_name, a.days_since_last_used, a.construct_year,
               a.acquisition_value, a.sap_equipment, a.serial_no, a.source, a.review
        from dim_asset a
        left join dim_workcell w on w.workcell_id = a.workcell_id
        """,
        {
            "asset_id": "Asset key.",
            "description": "What it is, as SAP / EST1C describe it (SCREWDRIVER_SEHAN_HD150, Universal GSM2 Placer ...).",
            "manufacturer": "Maker.",
            "model": "Model.",
            "equip_category": "SAP equipment category (T = tool / equipment).",
            "is_smart_torque": "true = a smart-torque tool tracked by EST1C (202 of them).",
            "torque_range": "Torque range for tools.",
            "lifecycle": "installed, scrapped, or NULL = unknown. Count installed only unless asked otherwise.",
            "est1c_status": "Running / Idle as EST1C last saw it (smart-torque tools).",
            "workcell": "Workcell = CUSTOMER that owns or uses it, where the register says.",
            "workcell_id": "Join key to v_workcell (NULL = unmapped).",
            "bay_raw": "Bay text as written in the register (layout scheme, mostly).",
            "bay_area": "Area (P1A ...) when parsed.",
            "location": "SAP location text - only 200 of 1,349 tools resolve to a real place (case 35).",
            "room": "SAP room.",
            "pc_name": "The workstation PC a smart-torque tool logs to.",
            "days_since_last_used": "Days since EST1C last saw the tool used.",
            "construct_year": "Year built.",
            "acquisition_value": "Acquisition value in SAP.",
            "sap_equipment": "SAP equipment number.",
            "serial_no": "Serial number.",
            "source": "est1c, sap or both.",
            "review": "A note when a person should confirm.",
        },
    ),
    "v_equipment": (
        """
        select e.equipment_id, e.name as equipment, e.is_machine, w.name as workcell, e.workcell_id, e.workcells, e.step, e.steps,
               e.bay, e.plant, e.first_seen, e.last_seen, e.scans, e.boards
        from dim_equipment e
        left join dim_workcell w on w.workcell_id = e.workcell_id
        """,
        {
            "equipment_id": "MES equipment id.",
            "equipment": "Equipment name as MES scans carry it (MYPENWAB0107_FT1, PACKOUT ...). A machine, a test station or a generic label.",
            "is_machine": "true = a named machine or test station dedicated to 1-3 workcells. false = a label: blank, a step name, or a generic station shared by many workcells (PACKOUT, FNI, OQA, LINK 1) - shared-line machines (AOP) also land here. Filter is_machine for 'which machine' questions.",
            "workcell": "The workcell it scanned most for.",
            "workcell_id": "Join key to v_workcell.",
            "workcells": "Distinct workcells it scanned for.",
            "step": "The step it scanned most.",
            "steps": "Distinct steps it scanned.",
            "bay": "The MES bay it scanned most in.",
            "plant": "Plant of those scans.",
            "first_seen": "First scan date.",
            "last_seen": "Last scan date - a machine not seen for weeks may be idle, moved or retired.",
            "scans": "Scan rows, 9 Jul to 22 Aug 2026.",
            "boards": "Distinct boards it scanned.",
        },
    ),
})
