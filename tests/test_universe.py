"""Jabil Universe — Phase 1 acceptance tests.

Every table in the universe gets its assertions BEFORE its build script. These are
the facts the Foundational Document and the gotchas register say must hold; if a
build produces a table where one of them is false, the build is wrong, not the test.

Run: python tests/test_universe.py          (no pytest needed)
  or python -m pytest tests/test_universe.py (if pytest is installed)
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from modules.universe import config as U  # noqa: E402
from modules.universe import registry as R  # noqa: E402


def _q(sql: str):
    con = duckdb.connect()
    try:
        for name, path in U.UNIVERSE_MART.items():
            if path.exists():
                con.execute(f"create view {name} as select * from read_parquet('{path.as_posix()}')")
        return con.execute(sql).fetchall()
    finally:
        con.close()


# ─── T1 · the module exists and answers /health ──────────────────────────────

def test_health_endpoint_answers():
    from fastapi.testclient import TestClient
    from api.main import app
    r = TestClient(app).get("/api/universe/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "tables" in body and "dim_workcell" in body["tables"], body


# ─── T2 · dim_workcell + workcell_alias ──────────────────────────────────────

def test_dim_workcell_promoted_with_all_active_rows():
    assert U.UNIVERSE_MART["dim_workcell"].exists(), "dim_workcell.parquet not built"
    (n_active,) = _q("select count(*) from dim_workcell where status = 'active'")[0]
    # 42 active rows in the August registry (37 customers + AOP + 4 support). Promote faithfully.
    assert n_active == 42, n_active


def test_every_configured_cycle_time_customer_resolves():
    """Rule 5: never join on a raw workcell name — but every configured name MUST resolve."""
    from modules.cycle_time.config import CT_CUSTOMERS
    misses = [c["customer"] for c in CT_CUSTOMERS if R.resolve(c["customer"]) is None]
    assert not misses, f"unresolved CT_CUSTOMERS names: {misses}"


def test_keysight_carries_both_mes_ids():
    """Case 3: one workcell, two MES ids — a column cannot hold two, the alias table can."""
    rows = _q("""
        select a.value, w.name
        from workcell_alias a join dim_workcell w on w.workcell_id = a.workcell_id
        where a.system = 'mes' and a.value in ('7', '114')
    """)
    assert {v for v, _ in rows} == {"7", "114"}, rows
    assert {n for _, n in rows} == {"KEYSIGHT"}, rows


def test_alias_system_value_is_unique():
    dups = _q("select system, value, count(*) c from workcell_alias group by 1, 2 having c > 1")
    assert not dups, dups[:5]


def test_aop_is_a_shared_line_not_a_customer():
    rows = _q("select entity_type from dim_workcell where name = 'AOP'")
    assert rows == [("shared_line",)], rows


def test_families_are_unverified_so_parent_id_is_null():
    """§8.1 #14: roots and subs are a proposal, not a fact. Keep the proposal, do not act on it."""
    (n_set,) = _q("select count(*) from dim_workcell where parent_id is not null")[0]
    (n_prop,) = _q("select count(*) from dim_workcell where parent_id_proposed is not null")[0]
    assert n_set == 0, n_set
    assert n_prop > 0, "the August proposal should be kept as parent_id_proposed"


def test_alias_conflicts_are_surfaced_not_resolved():
    """The August alias table mixes two meanings: "this spelling belongs to this
    workcell" and "this customer's cycle-time data folds into that workcell".
    Eight spellings point at two ids. The canonical row wins for resolve(); the
    conflict is recorded, never silently picked."""
    assert R.resolve("Tellabs") == 44, R.resolve("Tellabs")          # the Tellabs row, not INFINERA NEW
    # K_CTEC is a registered workcell (row 42); the cycle_time alias folds it into
    # KEYSIGHT (6) — a family claim, and families are unverified (§8.1 #14). So the
    # canonical row wins and the fold is recorded as a conflict, not applied.
    assert R.resolve("K_CTEC") == 42, R.resolve("K_CTEC")
    rows = _q("select spelling, ids from workcell_alias_conflict order by spelling")
    assert len(rows) >= 7, rows
    by = {r[0]: set(r[1]) for r in rows}
    assert by.get("TELLABS") == {44, 101}, by.get("TELLABS")
    assert by.get("KCTEC") == {6, 42}, by.get("KCTEC")


def test_plant_is_two_facts_physical_and_governing():
    """'Which plant' is two questions. MICRON SIG sits in BK and is run by P1."""
    row = _q("select plant_physical, plant_governing from dim_workcell where name = 'MICRON SIG'")
    assert row == [("BK", "P1")], row
    row = _q("select plant_physical, plant_governing, region from dim_workcell where name = 'LAM RESEARCH'")
    assert row == [("P1", "P1", "Penang Island")], row
    row = _q("select plant_physical, plant_governing, region from dim_workcell where name = 'Tellabs'")
    assert row == [("BK", "BK", "Batu Kawan")], row


# ─── T4 · dim_model (assembly × revision) ───────────────────────────────────

def test_dim_model_is_workcell_and_assembly_together():
    """A model is (workcell, assembly) TOGETHER — an assembly alone is half an identity."""
    assert U.UNIVERSE_MART["dim_model"].exists(), "dim_model.parquet not built"
    dups = _q("select workcell_id, match_key, count(*) c from dim_model group by 1, 2 having c > 1")
    assert not dups, dups[:5]
    (n,) = _q("select count(*) from dim_model")[0]
    assert n >= 167_000, n                                  # the August registry, promoted faithfully


def test_every_model_points_at_a_real_workcell():
    orphans = _q("""select count(*) from dim_model m
                    left join dim_workcell w on w.workcell_id = m.workcell_id
                    where m.workcell_id is not null and w.workcell_id is null""")
    assert orphans == [(0,)], orphans


def test_revisions_hang_off_models():
    """BOM and route hang off the revision, not the assembly (case 14)."""
    assert U.UNIVERSE_MART["dim_model_revision"].exists()
    dups = _q("select model_id, revision, count(*) c from dim_model_revision group by 1, 2 having c > 1")
    assert not dups, dups[:5]
    orphans = _q("""select count(*) from dim_model_revision r
                    left join dim_model m on m.model_id = r.model_id where m.model_id is null""")
    assert orphans == [(0,)], orphans


# ─── T5 · fact_scan — one row per board × step ───────────────────────────────

def test_fact_scan_is_one_row_per_board_step_scan():
    assert U.UNIVERSE_MART["fact_scan"].exists(), "fact_scan.parquet not built"
    (n,) = _q("select count(*) from fact_scan")[0]
    # The August pull is 19,841,768 raw rows; 1,094,216 are duplicate keys from
    # overlapping hourly windows. The truth is the deduped count.
    assert n >= 18_700_000, n
    dups = _q("""select count(*) from (select wip_id, step, step_instance, completed_at_utc, count(*) c
                 from fact_scan group by 1,2,3,4 having c > 1)""")
    assert dups == [(0,)], dups


def test_every_scan_points_at_a_workcell_row_even_unknown():
    """Customer_ID = 0 is an answer (case 6): an UNKNOWN workcell row, never a NULL key."""
    nulls = _q("select count(*) from fact_scan where workcell_id is null")
    assert nulls == [(0,)], nulls
    orphans = _q("""select count(*) from fact_scan f left join dim_workcell w on w.workcell_id = f.workcell_id
                    where w.workcell_id is null""")
    assert orphans == [(0,)], orphans
    unknown_row = _q("select name, entity_type from dim_workcell where workcell_id = 0")
    assert unknown_row == [("UNKNOWN", "unknown")], unknown_row    # the row exists even when nothing lands on it


def test_every_scan_model_resolves_or_is_null():
    orphans = _q("""select count(*) from fact_scan f left join dim_model m on m.model_id = f.model_id
                    where f.model_id is not null and m.model_id is null""")
    assert orphans == [(0,)], orphans


def test_local_time_is_utc_plus_8_and_shift_follows_it():
    """Case 49: convert before assigning shift, or every boundary vanishes."""
    bad_tz = _q("select count(*) from fact_scan where completed_at_local <> completed_at_utc + interval 8 hour")
    assert bad_tz == [(0,)], bad_tz
    bad_shift = _q("""select count(*) from fact_scan
                      where (hour(completed_at_local) between 7 and 18 and shift <> 2)
                         or (hour(completed_at_local) not between 7 and 18 and shift <> 3)""")
    assert bad_shift == [(0,)], bad_shift


def test_night_shift_after_midnight_belongs_to_the_previous_date():
    rows = _q("""select count(*) from fact_scan
                 where hour(completed_at_local) < 7 and shift_date <> cast(completed_at_local as date) - 1""")
    assert rows == [(0,)], rows
    rows = _q("""select count(*) from fact_scan
                 where hour(completed_at_local) >= 7 and shift_date <> cast(completed_at_local as date)""")
    assert rows == [(0,)], rows


# ─── T6 · terminal step per model, units out ─────────────────────────────────

def test_terminal_step_is_learned_per_model_from_history():
    """§8.1 #9 (refined): the last step is learned from the boards themselves —
    the step their final scan lands on — with the share of boards as confidence.
    PACKOUT is only the default when nothing was learned."""
    assert U.UNIVERSE_MART["model_terminal_step"].exists(), "model_terminal_step.parquet not built"
    dups = _q("select model_id, count(*) c from model_terminal_step group by 1 having c > 1")
    assert not dups, dups[:5]
    # Learning needs history: a model with < TERMINAL_MIN_BOARDS boards in the window
    # falls back to PACKOUT with learned = false — by design, not a failure. Among
    # models WITH enough history, nine in ten must learn a step.
    (n_enough,) = _q(f"select count(*) from model_terminal_step where boards >= {U.TERMINAL_MIN_BOARDS}")[0]
    (n_learned,) = _q(f"select count(*) from model_terminal_step where learned and boards >= {U.TERMINAL_MIN_BOARDS}")[0]
    assert n_learned / n_enough >= 0.9, f"{n_learned}/{n_enough} = {n_learned / n_enough:.3f}"
    thin = _q(f"select count(*) from model_terminal_step where boards < {U.TERMINAL_MIN_BOARDS} and (learned or terminal_step <> '{U.DEFAULT_TERMINAL_STEP}')")
    assert thin == [(0,)], thin
    kinds = {k for (k,) in _q("select distinct terminal_kind from model_terminal_step")}
    assert kinds <= {"packout", "link", "other"}, kinds


def test_units_out_counts_a_board_once_at_its_terminal_step():
    """Case 48: counting scan rows double-counts rework. A unit = one board, once."""
    assert U.UNIVERSE_MART["fact_unit_out"].exists(), "fact_unit_out.parquet not built"
    dups = _q("select wip_id, model_id, count(*) c from fact_unit_out group by 1, 2 having c > 1")
    assert not dups, dups[:5]
    (n_units,) = _q("select count(*) from fact_unit_out")[0]
    (n_scans,) = _q("select count(*) from fact_scan")[0]
    assert 0 < n_units < n_scans, (n_units, n_scans)


def _august_keysight_packout_units() -> int:
    return duckdb.connect().execute(
        f"select sum(units_out) from read_parquet('{(U.REGISTRY_DIR / 'production_out.parquet').as_posix()}') "
        "where try_cast(workcell_id as bigint) = 6").fetchone()[0]


def test_fact_scan_reproduces_the_august_packout_count():
    """Promotion check: August counted distinct boards at PACKOUT per (date, shift,
    model, bay). Recomputing that definition from fact_scan must land within 1 % —
    otherwise the dedupe lost boards."""
    aug = _august_keysight_packout_units()
    (recomputed,) = _q("""
        select count(*) from (
          select distinct wip_id, model_id, date, shift_name_raw, bay_id
          from fact_scan where workcell_id = 6 and step = 'PACKOUT')""")[0]
    assert aug and abs(recomputed - aug) / aug <= 0.01, f"recomputed {recomputed} vs august {aug}"


def test_units_out_reconciles_with_august_once_double_counting_is_added_back():
    """Case 48: we count a board once. August counted it again on every (date,
    shift, bay) it re-scanned PACKOUT. For KEYSIGHT models whose terminal step IS
    PACKOUT: ours + August's extra counts = August's number, within 1 %. Models
    ending at LINK or elsewhere are excluded here and reported separately — they
    are an open question, not a tolerance."""
    (ours,) = _q("""select count(*) from fact_unit_out u
                    join model_terminal_step t on t.model_id = u.model_id
                    where u.workcell_id = 6 and t.terminal_step = 'PACKOUT'""")[0]
    (extra,) = _q("""select coalesce(sum(n_groups - 1), 0) from (
                       select s.wip_id, s.model_id, count(distinct (s.date, s.shift_name_raw, s.bay_id)) n_groups
                       from fact_scan s join model_terminal_step t on t.model_id = s.model_id
                       where s.workcell_id = 6 and s.step = 'PACKOUT' and t.terminal_step = 'PACKOUT'
                       group by 1, 2)""")[0]
    aug_packout_models = duckdb.connect().execute(f"""
        select sum(a.units_out) from read_parquet('{(U.REGISTRY_DIR / 'production_out.parquet').as_posix()}') a
        join read_parquet('{U.UNIVERSE_MART['model_terminal_step'].as_posix()}') t on t.model_id = a.model_id
        where try_cast(a.workcell_id as bigint) = 6 and t.terminal_step = 'PACKOUT'""").fetchone()[0]
    assert abs(ours + extra - aug_packout_models) / aug_packout_models <= 0.01,         f"ours {ours} + extra {extra} = {ours + extra} vs august {aug_packout_models}"


# ─── T7 · the OLE proof — paid hours, SMH, and a reconciliation with the OLE module ──

def test_fact_paid_hours_is_one_row_per_person_shift_and_points_at_workcells():
    assert U.UNIVERSE_MART["fact_paid_hours"].exists(), "fact_paid_hours.parquet not built"
    dups = _q("""select count(*) from (select employee_no, date, shift, workcell_id, sub_workcell_raw, count(*) c
                 from fact_paid_hours group by 1,2,3,4,5 having c > 1)""")
    assert dups == [(0,)], dups
    orphans = _q("""select count(*) from fact_paid_hours f left join dim_workcell w on w.workcell_id = f.workcell_id
                    where w.workcell_id is null""")
    assert orphans == [(0,)], orphans
    (neg,) = _q("select count(*) from fact_paid_hours where paid_hours < 0")[0]
    assert neg == 0, neg


def test_smh_is_one_standard_per_model_and_stage():
    """SMH — standard man-hours per unit — is the earned-hours input to OLE."""
    assert U.UNIVERSE_MART["dim_smh"].exists(), "dim_smh.parquet not built"
    dups = _q("select workcell_id, model_id, scan_stage, count(*) c from dim_smh group by 1,2,3 having c > 1")
    assert not dups, dups[:5]
    (bad,) = _q("select count(*) from dim_smh where smh_per_unit is null or smh_per_unit <= 0")[0]
    assert bad == 0, bad


def test_ole_from_the_universe_reconciles_with_the_ole_module():
    """The proof. OLE = Σ(units_out × SMH) ÷ Σ paid_hours, per workcell per ISO
    week, computed from universe tables only, set beside the OLE module's own
    weekly number for the weeks both cover. Every delta over 2 points carries a
    computed reason — the point is not that they agree, it is that every
    disagreement is explained."""
    assert U.UNIVERSE_MART["ole_reconciliation"].exists(), "ole_reconciliation.parquet not built"
    rows = _q("""select workcell, iso_week, ole_universe, ole_module, delta_pts, reason
                 from ole_reconciliation where ole_module is not null""")
    assert len(rows) >= 10, len(rows)
    unexplained = [r for r in rows if abs(r[4]) > 2 and not (r[5] or "").strip()]
    assert not unexplained, unexplained[:5]
    # and the universe number is a real OLE, not a ratio of nothing
    (n_real,) = _q("select count(*) from ole_reconciliation where ole_universe between 1 and 200")[0]
    assert n_real >= 10, n_real


# ─── T8 · semantic views — the layer a model (or a person) reads ─────────────

def test_views_carry_meaning_in_column_comments():
    """A view without column comments is a column list; a model cannot know that
    workcell = customer or that units are boards-once from the names alone."""
    from modules.universe import views
    con = views.connect()
    try:
        for v in ("v_workcell", "v_units_out_daily", "v_ole_weekly"):
            cols = con.execute(f"select column_name, comment from duckdb_columns() where table_name = '{v}'").fetchall()
            assert cols, f"{v} missing"
            missing = [c for c, cm in cols if not (cm or "").strip()]
            assert not missing, f"{v}: columns without a comment: {missing}"
    finally:
        con.close()


def test_pool_q1_list_all_workcells_from_the_view_only():
    """Pool Q1. The view must say WHICH count — so it exposes status and entity_type,
    and the active-customer count equals the registry's."""
    from modules.universe import views
    con = views.connect()
    try:
        (n,) = con.execute("select count(*) from v_workcell where status = 'active' and entity_type = 'customer'").fetchone()
        assert n == 37, n
        row = con.execute("select plant_physical, plant_governing from v_workcell where workcell = 'MICRON SIG'").fetchone()
        assert row == ("BK", "P1"), row
    finally:
        con.close()


def test_pool_q5_output_trend_for_one_model_from_the_view_only():
    """Pool Q5. Output trend of one model in one workcell, by day — a query over
    v_units_out_daily alone, no parquet paths, no joins the asker must know."""
    from modules.universe import views
    con = views.connect()
    try:
        rows = con.execute("""
            select date, units_out from v_units_out_daily
            where workcell = 'KEYSIGHT' and assembly = (
              select assembly from v_units_out_daily where workcell = 'KEYSIGHT'
              group by 1 order by sum(units_out) desc limit 1)
            order by date""").fetchall()
        assert len(rows) >= 20, len(rows)
        assert all(u > 0 for _, u in rows), rows[:3]
    finally:
        con.close()


# ═══ PHASE 2 — waves 2 and 3, from disk ══════════════════════════════════════

# ─── Wave 2 · people ─────────────────────────────────────────────────────────

def test_dim_employee_scope_is_a_real_fact():
    """Case 31: department ≠ workcell; a site-scope engineer is not missing data."""
    assert U.UNIVERSE_MART["dim_employee"].exists(), "dim_employee.parquet not built"
    dups = _q("select employee_id, count(*) c from dim_employee group by 1 having c > 1")
    assert not dups, dups[:5]
    scopes = {r[0] for r in _q("select distinct scope from dim_employee")}
    assert scopes <= {"workcell", "site"}, scopes
    (n_site,) = _q("select count(*) from dim_employee where scope = 'site'")[0]
    assert n_site > 0
    orphans = _q("""select count(*) from dim_employee e left join dim_workcell w on w.workcell_id = e.workcell_id
                    where e.workcell_id is not null and w.workcell_id is null""")
    assert orphans == [(0,)], orphans


def test_paid_hours_employees_resolve_to_people_or_are_counted():
    """A paid-hours row whose person is unknown is reported, never dropped."""
    # 877 payroll numbers are not in HR at all — agency / contract codes (WHL…, NWL…).
    # They are 3.1% of HOURS, and hours are what OLE divides by; so the bar is hours.
    rows = _q("""select sum(p.paid_hours) filter (where e.employee_id is not null), sum(p.paid_hours),
                        count(distinct p.employee_no) filter (where e.employee_id is null)
                 from fact_paid_hours p left join dim_employee e on e.payroll_no = p.employee_no""")
    matched_hours, total_hours, unmatched_people = rows[0]
    assert matched_hours / total_hours >= 0.95, f"{matched_hours}/{total_hours}"
    assert unmatched_people < 1000, unmatched_people


def test_dim_department_has_a_kind_and_parents_resolve():
    assert U.UNIVERSE_MART["dim_department"].exists()
    dups = _q("select department_id, count(*) c from dim_department group by 1 having c > 1")
    assert not dups, dups
    orphans = _q("""select count(*) from dim_department d left join dim_department p on p.department_id = d.parent_id
                    where d.parent_id is not null and p.department_id is null""")
    assert orphans == [(0,)], orphans


# ─── Wave 3 · process, studies, routes, demand ───────────────────────────────

def test_dim_process_has_three_levels_and_aliases():
    """Case 21: kind → alias (the identity) → MES step. The alias is the row."""
    assert U.UNIVERSE_MART["dim_process"].exists(), "dim_process.parquet not built"
    dups = _q("select process_id, count(*) c from dim_process group by 1 having c > 1")
    assert not dups, dups[:5]
    (n,) = _q("select count(*) from dim_process")[0]
    assert n >= 1_000, n
    (n_kind,) = _q("select count(distinct process_kind) from dim_process where process_kind is not null")[0]
    assert n_kind >= 100, n_kind
    dups = _q("select system, value, count(*) c from process_alias group by 1, 2 having c > 1")
    assert not dups, dups[:5]
    orphans = _q("""select count(*) from process_alias a left join dim_process p on p.process_id = a.process_id
                    where p.process_id is null""")
    assert orphans == [(0,)], orphans


def test_cycle_time_studies_are_append_only_rows_with_a_status():
    """§8.1 #7: a study is an event with a status; absence is a value (case 41)."""
    assert U.UNIVERSE_MART["fact_cycle_time_study"].exists(), "fact_cycle_time_study.parquet not built"
    dups = _q("select study_id, count(*) c from fact_cycle_time_study group by 1 having c > 1")
    assert not dups, dups[:3]
    (n,) = _q("select count(*) from fact_cycle_time_study")[0]
    assert n >= 4_400_000, n
    statuses = {r[0] for r in _q("select distinct ct_status from fact_cycle_time_study")}
    assert statuses <= {"measured", "inherited", "estimated", "missing", "disputed"}, statuses
    cols = {r[0] for r in _q("select column_name from (describe fact_cycle_time_study)")}
    assert "quote" not in cols, "case 17: the dead quote column must not be promoted"
    orphans = _q("""select count(*) from fact_cycle_time_study s left join dim_model m on m.model_id = s.model_id
                    where s.model_id is not null and m.model_id is null""")
    assert orphans == [(0,)], orphans


def test_measured_cycle_time_is_a_separate_table_never_a_study():
    """Case 51: MES scan deltas are elapsed time. Separate table, provenance on every row."""
    assert U.UNIVERSE_MART["fact_cycle_time_measured"].exists()
    (bad,) = _q("select count(*) from fact_cycle_time_measured where provenance <> 'mes_scan_delta'")[0]
    assert bad == 0, bad
    (n,) = _q("select count(*) from fact_cycle_time_measured")[0]
    assert n >= 80_000, n


def test_route_steps_are_ordered_per_model_and_line():
    """Pool Q4: every step this model goes through, in order."""
    assert U.UNIVERSE_MART["fact_route"].exists(), "fact_route.parquet not built"
    dups = _q("select model_id, line_id, step_order, count(*) c from fact_route group by 1, 2, 3 having c > 1")
    assert not dups, dups[:5]
    rows = _q("""select step_order, process_alias from fact_route
                 where model_id = (select model_id from fact_route group by 1 order by count(*) desc limit 1)
                   and line_id = (select line_id from fact_route where model_id = (select model_id from fact_route group by 1 order by count(*) desc limit 1) limit 1)
                 order by step_order""")
    assert len(rows) >= 3 and [r[0] for r in rows] == sorted(r[0] for r in rows), rows[:5]
    (unmapped,) = _q("select count(*) from fact_route where process_id is null")[0]
    (total,) = _q("select count(*) from fact_route")[0]
    assert unmapped / total < 0.5, f"{unmapped}/{total} route steps map to no process"


def test_demand_joins_on_the_part_number_never_the_workcell_name():
    """Case 18: joining on workcell silently dropped ~1.9M units. The universe
    joins on the model key; workcell comes through the registry."""
    assert U.UNIVERSE_MART["fact_demand"].exists(), "fact_demand.parquet not built"
    dups = _q("""select count(*) from (select workcell_id, model_id, period_start, period_type, source, as_of, count(*) c
                 from fact_demand group by all having c > 1)""")
    assert dups == [(0,)], dups
    (resolved, total) = _q("""select sum(qty) filter (where m.model_id is not null), sum(qty)
                              from fact_demand d left join dim_model m on m.model_id = d.model_id""")[0]
    assert resolved / total >= 0.95, f"{resolved}/{total}"
    (no_wc,) = _q("select count(*) from fact_demand where workcell_id is null")[0]
    assert no_wc == 0, no_wc


# ─── Views and the temporary history ─────────────────────────────────────────

def test_fpy_view_is_loop_one_pass_over_tested():
    """Pool Q7, the 'where': FPY = P ÷ (P + F) at test steps, first loop only (case 48)."""
    from modules.universe import views
    con = views.connect()
    try:
        (bad,) = con.execute("select count(*) from v_fpy_daily where fpy < 0 or fpy > 1").fetchone()
        assert bad == 0, bad
        (n,) = con.execute("select count(*) from v_fpy_daily").fetchone()
        assert n > 1000, n
        (bad,) = con.execute("select count(*) from v_fpy_daily where boards_tested < boards_passed").fetchone()
        assert bad == 0, bad
    finally:
        con.close()


def test_share_production_is_kept_separate_and_labelled():
    """Case 48: share quantities and boards count differently. A second opinion,
    never merged; the view names the source on every row."""
    assert U.UNIVERSE_MART["fact_production_share"].exists()
    (bad,) = _q("select count(*) from fact_production_share where source <> 'share' or source is null")[0]
    assert bad == 0, bad
    (resolved, total) = _q("select count(*) filter (where workcell_id <> 0), count(*) from fact_production_share")[0]
    assert resolved / total >= 0.95, f"{resolved}/{total}"
    from modules.universe import views
    con = views.connect()
    try:
        sources = {r[0] for r in con.execute("select distinct source from v_output_daily").fetchall()}
        assert sources == {"boards", "share"}, sources
        lo, hi = con.execute("select min(date), max(date) from v_output_daily where source = 'share'").fetchone()
        assert str(lo) < "2026-07-01", lo                      # the share history reaches further back than the scans
    finally:
        con.close()


def test_every_view_has_every_column_commented():
    from modules.universe import views
    con = views.connect()
    try:
        for v in views.VIEWS:
            cols = con.execute(f"select column_name, comment from duckdb_columns() where table_name = '{v}'").fetchall()
            assert cols, f"{v} missing"
            missing = [c for c, cm in cols if not (cm or "").strip()]
            assert not missing, f"{v}: {missing}"
    finally:
        con.close()


# ─── The refresh — built now, run when the VPN is back ──────────────────────

def test_refresh_rebuilds_fact_scan_from_the_raw_pulls_exactly():
    """The 30 raw hourly-pull CSVs already on disk must reproduce Phase 1's
    fact_scan to the row — same parse, same dedupe. Slow (3.3 GB); it is the
    acceptance test for the refresh path, so it stays."""
    from modules.universe.pipeline import refresh
    n = refresh.count_from_raw(U.REGISTRY_DIR / "wipscan")
    assert n == 18_747_552, n


# ═══ PHASE 3 — the first modules as queries ═══════════════════════════════════

def test_ole_daily_view_computes_from_universe_tables_only():
    """P3.1: OLE per (workcell, date, shift) from boards × SMH ÷ paid hours."""
    from modules.universe import views
    con = views.connect()
    try:
        (n,) = con.execute("select count(*) from v_ole_daily where ole is not null").fetchone()
        assert n >= 200, n
        (bad,) = con.execute("select count(*) from v_ole_daily where ole < 0").fetchone()
        assert bad == 0, bad
        cols = {r[0] for r in con.execute("select column_name from duckdb_columns() where table_name = 'v_ole_daily'").fetchall()}
        assert {"workcell", "date", "shift", "units", "earned_smh", "paid_hours", "ole", "smh_policy"} <= cols, cols
    finally:
        con.close()


def test_smh_estimation_policy_explains_the_module_gap():
    """Case 62, corrected by this very test. The OLE module HAS an estimate switch
    (OLE_SMH_FALLBACK=avg) but runs with it OFF — its estimated_output_smh is 0.
    And the estimate is not a safe proxy: under policy = 'estimate' ASP (FORTIVE)
    lands FURTHER from the module than under 'zero' on every full week (W29:
    297% vs 52% vs module 45%), because the units without a standard are
    low-SMH models. So 'zero' stays the default, and the register says why."""
    import duckdb as _d
    from modules.universe.pipeline import build
    rows = build.ole_policy_comparison(workcell="ASP (FORTIVE)", weeks=(29, 30, 31))
    assert len(rows) == 3, rows
    worse = [r for r in rows if abs(r["delta_estimate"]) > abs(r["delta_zero"])]
    assert len(worse) == 3, rows
    (est,) = _d.connect().execute(
        "select coalesce(sum(estimated_output_smh), 0) from read_parquet('data/mart/ole/ole_computed.parquet')").fetchone()
    assert est == 0, f"the module's estimate switch is on ({est} SMH estimated) — the register must say so"


def test_model_completion_reconciles_with_the_cycle_time_module():
    """P3.2: completion per (workcell, model) from fact_route + studies, beside the
    Cycle Time module's completion_status_v2. Every coverage gap > 10 points carries
    a computed reason."""
    assert U.UNIVERSE_MART["completion_reconciliation"].exists(), "completion_reconciliation.parquet not built"
    # The comparable population is the models the module actually GRADED
    # (complete + incomplete, ~6.3k). Its 33k not_in_mes rows carry no coverage;
    # they appear here with a reason, not a number.
    rows = _q("""select workcell, assembly, coverage_universe, coverage_module, delta, reason
                 from completion_reconciliation where coverage_module is not null""")
    assert len(rows) >= 5_000, len(rows)
    (n_not_in_mes,) = _q("select count(*) from completion_reconciliation where status_module = 'not_in_mes' and reason like 'module: not_in_mes%'")[0]
    assert n_not_in_mes > 10_000, n_not_in_mes
    unexplained = [r for r in rows if r[4] is not None and abs(r[4]) > 0.10 and not (r[5] or "").strip()]
    assert not unexplained, unexplained[:5]
    (agree,) = _q("select count(*) from completion_reconciliation where abs(delta) <= 0.05")[0]
    assert agree >= 0.5 * len(rows), f"only {agree} of {len(rows)} within 5 points"


def test_authored_seeds_carry_provenance_and_are_marked_authored():
    """Case 54: some entities must be CREATED, not extracted. Every row says where
    it came from; every table says it is authored."""
    for t in ("auth_equipment_capacity", "auth_playbook", "auth_process_group", "auth_trolley_type"):
        assert U.UNIVERSE_MART[t].exists(), f"{t} not built"
        (bad,) = _q(f"select count(*) from {t} where provenance is null or provenance = '' or not authored")[0]
        assert bad == 0, (t, bad)
        (n,) = _q(f"select count(*) from {t}")[0]
        assert n > 0, t


# ─── T3 · dim_calendar + dim_shift ───────────────────────────────────────────

def test_fiscal_year_starts_in_september():
    row = _q("select fiscal_year, fiscal_quarter from dim_calendar where date = date '2026-09-01'")
    assert row == [(2027, 1)], row
    row = _q("select fiscal_year, fiscal_quarter from dim_calendar where date = date '2026-08-31'")
    assert row == [(2026, 4)], row


def test_iso_week_53_exists_and_lags_the_calendar_year():
    row = _q("select iso_year, iso_week from dim_calendar where date = date '2027-01-01'")
    assert row == [(2026, 53)], row


def test_only_shifts_2_and_3_carry_production():
    rows = _q("select shift, start_time, carries_production from dim_shift order by shift")
    assert [(s, str(st)[:5], p) for s, st, p in rows] == [
        (1, "08:00", False), (2, "07:00", True), (3, "19:00", True)], rows


def main() -> None:
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:                     # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {str(e)[:200]}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
