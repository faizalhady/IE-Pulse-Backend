"""Headcount resolution for position/customer — the bug this replaces was three
people saved with NULLs because the client did not send the fields.

Run: python tests/test_access_hc.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl

from api.routers import access


def _fake_hc(tmp: Path) -> Path:
    """A two-person HC.xlsx with the real column headers."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Headcount"
    ws.append(["Legal Name", "Employee ID", "NT Account Name", "Primary Work Email",
               "Business Title", "Customer", "Dept"])
    ws.append(["Ada Numeric", "123755", "123755", "Ada_N@Jabil.com",
               "IE Section Manager", "Non-Workcell", "IE"])
    ws.append(["Bea Named", "999001", "BeaN2", "Bea_N@Jabil.com",
               "IE Engineer Sr", "KEYSIGHT", "IE"])
    p = tmp / "HC.xlsx"
    wb.save(p)
    return p


def main() -> None:
    tmp = Path(__file__).parent / "_tmp_access_hc"
    tmp.mkdir(exist_ok=True)
    access.HC_XLSX = _fake_hc(tmp)
    access._hc_index.cache_clear()

    # 1. numeric NTID, 2. account-name NTID, 3. email fallback — user_access.ntid
    #    genuinely holds both spellings ('123755' and 'LawC2').
    assert access.hc_person("123755") == ("IE Section Manager", "Non-Workcell")
    assert access.hc_person("bean2") == ("IE Engineer Sr", "KEYSIGHT"), "casefold"
    assert access.hc_person("Bea_N@Jabil.com") == ("IE Engineer Sr", "KEYSIGHT")
    assert access.hc_person("nobody") == (None, None)
    assert access.hc_person(None) == (None, None)

    # A missing/locked spreadsheet must not raise — the roster stays up.
    access.HC_XLSX = tmp / "gone.xlsx"
    access._hc_index.cache_clear()
    assert access.hc_person("123755") == (None, None), "missing file must not raise"

    # ── The actual regression: a client that sends nothing must not blank a row.
    access.HC_XLSX = _fake_hc(tmp)
    access._hc_index.cache_clear()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE user_access (
        ntid TEXT PRIMARY KEY, name TEXT, email TEXT, level TEXT, apps TEXT,
        position TEXT, customer TEXT, added_by TEXT,
        created_at TEXT DEFAULT (datetime('now')), updated_at TEXT)""")

    def save(ntid, name, email, position, customer):
        """upsert_user's body, minus the FastAPI plumbing — the resolver and the
        SQL are imported from the module, so editing either fails this test."""
        pos, cust = access.resolve_hc(ntid, email, position, customer)
        conn.execute(access.UPSERT_SQL,
                     (ntid, name, email, pos, cust, "viewer", "all", "test"))

    def row(ntid):
        return conn.execute("SELECT * FROM user_access WHERE ntid = ?", (ntid,)).fetchone()

    # An OLD client: sends position=None, customer=None. Used to write NULLs.
    save("123755", "Ada Numeric", "Ada_N@Jabil.com", None, None)
    assert row("123755")["position"] == "IE Section Manager", "add must fill from HC"
    assert row("123755")["customer"] == "Non-Workcell"

    # Saving again from that same old client must not undo it.
    save("123755", "Ada Numeric", "Ada_N@Jabil.com", None, None)
    assert row("123755")["position"] == "IE Section Manager", "re-save must not wipe"

    # Someone headcount has never heard of: keep whatever the client offered,
    # and a later blank save must not erase it.
    save("CONTRACT1", "Temp Person", "temp@x.com", "Contractor", "ACME")
    assert row("CONTRACT1")["position"] == "Contractor"
    save("CONTRACT1", "Temp Person", "temp@x.com", None, None)
    assert row("CONTRACT1")["position"] == "Contractor", "COALESCE must protect it"
    assert row("CONTRACT1")["customer"] == "ACME"

    # ── Self-heal: a row already NULL fills itself when the page is opened.
    conn.execute("INSERT INTO user_access (ntid, name, email, level, apps) "
                 "VALUES ('BeaN2','Bea Named','Bea_N@Jabil.com','viewer','all')")
    users = [dict(r) for r in conn.execute("SELECT * FROM user_access WHERE ntid='BeaN2'")]
    access._heal_missing_hc(conn, users)
    assert row("BeaN2")["position"] == "IE Engineer Sr", "heal must persist"
    assert users[0]["customer"] == "KEYSIGHT", "heal must update the response too"

    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    print("test_access_hc: all assertions passed")


if __name__ == "__main__":
    main()
