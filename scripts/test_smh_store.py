"""
scripts/test_smh_store.py
─────────────────────────
Self-check for smh_store. Runs against a throwaway DB, never the real one.

  python -m scripts.test_smh_store

Covers the rules that would silently corrupt an OLE number if they broke:
zero/blank rejection, duplicate create, audit trail on every mutation, and
load_lookup()'s column contract with compute.py.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.database as db                              # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smh_test_"))
    db.DB_PATH = tmp / "test.db"                        # redirect before anything connects
    db.init_db()

    from modules.ole import smh_store as s              # noqa: E402
    from modules.ole.smh_store import SmhError          # noqa: E402

    def raises(fn, *a, **k):
        try:
            fn(*a, **k)
        except SmhError:
            return True
        return False

    # ── create ────────────────────────────────────────────────────────────────
    row = s.create("ASP", "ASM-001", 1.25, by="4033375")
    assert row["smh_value"] == 1.25, row
    assert s.count_smh() == 1

    # zero and negative are rejected — compute.py cannot tell a stored 0 from a
    # missing row, so a 0 would silently zero out earned hours
    assert raises(s.create, "ASP", "ASM-ZERO", 0, by="x"), "zero must be rejected"
    # a decimal-place error looks populated but earns nothing — worse than absent
    assert raises(s.create, "ASP", "ASM-TINY", 4.761905e-10, by="x"), "sub-minimum must be rejected"
    assert raises(s.create, "ASP", "ASM-NEG", -1, by="x"), "negative must be rejected"
    assert raises(s.create, "ASP", "ASM-BLANK", "", by="x"), "blank must be rejected"
    assert raises(s.create, "ASP", "", 1.0, by="x"), "empty assembly must be rejected"
    assert s.count_smh() == 1, "rejected rows must not be stored"

    # duplicate create is an error, not a silent overwrite
    assert raises(s.create, "ASP", "ASM-001", 2.0, by="x"), "duplicate must be rejected"

    # ── update ────────────────────────────────────────────────────────────────
    row = s.update("ASP", "ASM-001", 2.5, by="1268287")
    assert row["smh_value"] == 2.5, row
    assert raises(s.update, "ASP", "NOPE", 1.0, by="x"), "update of missing row must fail"
    assert raises(s.update, "ASP", "ASM-001", 0, by="x"), "update to zero must be rejected"

    # ── load_lookup: the column contract compute.py joins on ──────────────────
    df = s.load_lookup()
    for col in ("workcell", "assembly", "smh_value", "scan_stage", "stage_label", "plant"):
        assert col in df.columns, f"load_lookup missing {col} — compute.py needs it"
    assert len(df) == 1

    # ── bulk upsert: blanks skipped, not stored as 0 ──────────────────────────
    res = s.upsert_many([
        {"workcell": "AOP1", "assembly": "A-1", "smh_value": 0.5},
        {"workcell": "AOP1", "assembly": "A-2", "smh_value": 0.0},    # blank cell in the .xls
        {"workcell": "AOP1", "assembly": "A-3", "smh_value": None},
        {"workcell": "ASP",  "assembly": "ASM-001", "smh_value": 3.0},  # existing -> update
    ], by="migration")
    assert res["created"] == 1, res
    assert res["updated"] == 1, res
    assert res["skipped"] == 2, res
    assert s.count_smh() == 2, "zero/None rows must not be stored"

    # re-running the same import is a no-op
    res2 = s.upsert_many([{"workcell": "AOP1", "assembly": "A-1", "smh_value": 0.5}], by="migration")
    assert res2 == {"created": 0, "updated": 0, "skipped": 0}, res2

    # ── delete ────────────────────────────────────────────────────────────────
    s.delete("AOP1", "A-1", by="4033375")
    assert s.count_smh() == 1
    assert raises(s.delete, "AOP1", "A-1", by="x"), "delete of missing row must fail"

    # ── audit survives the row ────────────────────────────────────────────────
    hist = s.history("AOP1", "A-1")
    actions = [h["action"] for h in hist]
    assert "delete" in actions and "import" in actions, actions
    deleted = next(h for h in hist if h["action"] == "delete")
    assert deleted["old_value"] == 0.5 and deleted["new_value"] is None, deleted
    assert deleted["changed_by"] == "4033375", deleted

    asp = s.history("ASP", "ASM-001")
    assert [h["action"] for h in asp] == ["update", "update", "create"], asp
    assert asp[-1]["new_value"] == 1.25 and asp[0]["new_value"] == 3.0, asp

    print("smh_store: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
