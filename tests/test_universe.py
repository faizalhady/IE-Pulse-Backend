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
