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
    (n_building,) = _q("select count(distinct model_id) from fact_scan where model_id is not null")[0]
    (n_learned,) = _q("select count(*) from model_terminal_step where learned and share >= 0.5")[0]
    assert n_learned / n_building >= 0.9, f"{n_learned}/{n_building} = {n_learned / n_building:.3f}"


def test_units_out_counts_a_board_once_at_its_terminal_step():
    """Case 48: counting scan rows double-counts rework. A unit = one board, once."""
    assert U.UNIVERSE_MART["fact_unit_out"].exists(), "fact_unit_out.parquet not built"
    dups = _q("select wip_id, model_id, count(*) c from fact_unit_out group by 1, 2 having c > 1")
    assert not dups, dups[:5]
    (n_units,) = _q("select count(*) from fact_unit_out")[0]
    (n_scans,) = _q("select count(*) from fact_scan")[0]
    assert 0 < n_units < n_scans, (n_units, n_scans)


def test_keysight_units_out_match_the_august_count_within_one_percent():
    """The August registry counted units at PACKOUT (production_out.parquet). The
    learned terminal step must land within 1 % of it for KEYSIGHT over the period."""
    aug = duckdb.connect().execute(
        f"select sum(units_out) from read_parquet('{(U.REGISTRY_DIR / 'production_out.parquet').as_posix()}') "
        "where try_cast(workcell_id as bigint) = 6").fetchone()[0]
    (ours,) = _q("select count(*) from fact_unit_out where workcell_id = 6")[0]
    assert aug and abs(ours - aug) / aug <= 0.01, f"universe {ours} vs august {aug} ({(ours - aug) / aug:+.2%})"


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
