"""
database.py
───────────
SQLite database for user-entered operational data.

Tables:
  downtime_logs   — supervisor-keyed production interruptions
  transfer_logs   — cross-workcell man-hour transfers
  saved_reports   — user-saved report content, keyed to their NTID

This is separate from the parquet mart (which is computed/read-only).
The SQLite file lives at data/operational.db and persists across restarts.
"""

import sqlite3

from core.paths import DATA_DIR

DB_PATH = DATA_DIR / "operational.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # safe concurrent reads
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS downtime_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                date        TEXT    NOT NULL,   -- YYYY-MM-DD
                shift       INTEGER NOT NULL,   -- 1=Day  2=Night  3=Overtime
                workcell    TEXT    NOT NULL,   -- matches WORKCELL_CONFIG key
                bay         TEXT,
                dept        TEXT    NOT NULL,
                code        TEXT    NOT NULL,
                dl_affected INTEGER NOT NULL DEFAULT 0,
                minutes     INTEGER NOT NULL,
                commentary  TEXT
            );

            CREATE TABLE IF NOT EXISTS transfer_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                date        TEXT    NOT NULL,   -- YYYY-MM-DD
                shift       INTEGER NOT NULL,   -- 1=Day  2=Night  3=Overtime
                from_wc     TEXT    NOT NULL,   -- source workcell
                to_wc       TEXT    NOT NULL,   -- destination workcell
                va_hc       INTEGER NOT NULL DEFAULT 0,
                va_hrs      REAL    NOT NULL DEFAULT 0,
                nva_hc      INTEGER NOT NULL DEFAULT 0,
                nva_hrs     REAL    NOT NULL DEFAULT 0
            );

            -- ponytail: ONE generic table, not one per module. `module` +
            -- `report_type` + a JSON `payload` means PPQT / Cycle Time / IPK
            -- reuse this with zero schema change — the cost is identical to an
            -- OLE-only table today. Deliberately SQLite, not parquet: this is
            -- user-entered data with row-level create/update/delete, which is
            -- exactly what parquet (columnar, rewrite-whole-file) is bad at.
            --
            -- Move to Postgres only when one of these is actually true:
            --   * more than one app server (SQLite is single-writer)
            --   * sustained concurrent writes (~10+/sec)
            --   * another host needs to hit the DB directly
            --   * you need to query INSIDE the JSON payload
            -- Until then this is a straight port, not a rewrite.
            -- `id` is the identity. `name` is just a field, so two reports may
            -- share a title (as in Google Docs) and renaming is an UPDATE of a
            -- known row rather than delete-old-insert-new keyed on a string.
            -- An earlier version had UNIQUE(module, report_type, owner_ntid,
            -- name); see _migrate_saved_reports below for the rebuild.
            CREATE TABLE IF NOT EXISTS saved_reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                module      TEXT    NOT NULL,   -- 'ole' | 'cycle_time' | ...
                report_type TEXT    NOT NULL,   -- '4q'
                name        TEXT    NOT NULL,   -- display name; NOT an identity
                owner_ntid  TEXT    NOT NULL,   -- from RetrieveUserInfoNoParam
                owner_name  TEXT,
                owner_email TEXT,
                payload     TEXT    NOT NULL,   -- JSON; for 4q = the Q3 plan only
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_saved_reports_owner
                ON saved_reports (module, report_type, owner_ntid);

            -- Access, modelled the way people actually describe it:
            -- ONE ROW PER PERSON, not one per grant.
            --
            -- The first cut stored a row per (person, scope) pair, which meant
            -- someone who led two workcells and admin'd an app appeared three
            -- times in the table with no obvious relationship between the rows.
            -- Splitting the person from the things they own fixes that: the
            -- list reads as people, and the detail hangs off them.
            --
            --   level   what they ARE          viewer < admin < super_admin < developer
            --   apps    where that applies     'all', or a CSV like 'ole,cycle_time'
            --   workcells (other table)        what they LEAD - zero or many
            --
            -- `level` is global authority; leading a workcell is a separate
            -- fact. Someone can lead three workcells and still be a viewer
            -- everywhere else, which is the common case.
            CREATE TABLE IF NOT EXISTS user_access (
                ntid        TEXT    PRIMARY KEY,
                name        TEXT,
                email       TEXT,
                level       TEXT    NOT NULL DEFAULT 'viewer'
                            CHECK (level IN ('viewer','admin','super_admin','developer')),
                -- 'all' rather than listing every app, so adding a module does
                -- not silently narrow anyone's existing access.
                apps        TEXT    NOT NULL DEFAULT 'all',
                -- Copied from headcount at the time they were added, not joined
                -- live: this backend runs as LocalSystem and cannot authenticate
                -- to AD_GET as a person, so the browser supplies them. Stale by
                -- design — re-save a person to refresh.
                position    TEXT,
                customer    TEXT,
                added_by    TEXT,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- Which workcells a person leads. Separate table because it is
            -- genuinely many-per-person, and because "who leads ASP?" should be
            -- an index lookup, not a string search through a CSV column.
            CREATE TABLE IF NOT EXISTS user_workcells (
                ntid        TEXT    NOT NULL,
                workcell    TEXT    NOT NULL,
                is_primary  INTEGER NOT NULL DEFAULT 0,   -- addressed first in email
                PRIMARY KEY (ntid, workcell)
            );
            CREATE INDEX IF NOT EXISTS idx_user_workcells_wc
                ON user_workcells (workcell);

            -- Notification opt-ins, keyed so a second report type costs a row
            -- rather than a migration. Today there is one key: 'ole_smh'.
            CREATE TABLE IF NOT EXISTS user_notifications (
                ntid        TEXT    NOT NULL,
                key         TEXT    NOT NULL,   -- 'ole_smh' | future report types
                enabled     INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (ntid, key)
            );

            -- Standard Man Hours per unit — the NUMERATOR of every OLE number.
            --
            -- Was ten HTML-disguised .xls files under data/raw/, re-parsed on
            -- every pipeline run. They are still accepted (bulk upload), but
            -- this table is now the source of truth: ingest_smh() reads here,
            -- not from disk. That is what makes SMH editable from the UI at
            -- all — an edit written to the parquet mart was overwritten by the
            -- next refresh.
            --
            -- An edit here is RETROACTIVE. compute.py rebuilds ole_computed
            -- from the whole production history each run, so adding an SMH
            -- value today changes last month's OLE too. That is intended: the
            -- standard hour was always that number, we just hadn't recorded it.
            --
            -- smh_value > 0 is enforced here, not merely validated in the
            -- router. A zero reads identically to "no SMH" in compute.py's
            -- LEFT JOIN, so a row storing 0 would be an invisible way to break
            -- a workcell's OLE. Deleting the row is the honest way to say "not
            -- known". smh_store.MIN_SMH additionally rejects decimal-place
            -- errors (values below 1e-4); that floor is business policy, so it
            -- lives in code where it can carry its reasoning, not in a CHECK.
            CREATE TABLE IF NOT EXISTS smh (
                workcell    TEXT NOT NULL,          -- WORKCELL_CONFIG key
                assembly    TEXT NOT NULL,          -- AssemblyNumber from MES
                smh_value   REAL NOT NULL CHECK (smh_value > 0),
                scan_stage  TEXT,                   -- SMT | Backend | BoxBuild
                stage_label TEXT,
                plant       TEXT,
                source      TEXT NOT NULL DEFAULT 'manual',  -- 'xls' | 'manual'
                updated_by  TEXT,                   -- ntid
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (workcell, assembly)
            );

            CREATE INDEX IF NOT EXISTS idx_smh_workcell ON smh (workcell);

            -- Append-only. SMH edits move the plant's headline number, so
            -- "who changed this and what was it before" has to survive the
            -- row being edited again or deleted outright.
            CREATE TABLE IF NOT EXISTS smh_audit (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                workcell   TEXT NOT NULL,
                assembly   TEXT NOT NULL,
                action     TEXT NOT NULL,           -- create | update | delete | import
                old_value  REAL,                    -- NULL on create/import
                new_value  REAL,                    -- NULL on delete
                changed_by TEXT,
                changed_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_smh_audit_row
                ON smh_audit (workcell, assembly, changed_at DESC);

            -- An engineer's answer to "what IS this MES step?".
            --
            -- IEDB's step name is a free-text box, so MES scans names that no
            -- mapping covers - 105 of them across 14 workcells. They cannot be
            -- derived: matching by name is 38% right, by neighbouring scan 27%,
            -- by bay 55%. Only someone who works the line knows, and until now
            -- there was nowhere for them to say so.
            --
            -- answer:  'mapped'    -> iedb_alias names the IEDB process
            --          'non_iedb'  -> real work, but IEDB never prices it
            --                         (rework, RMA, debug, handling)
            --          'unknown'   -> asked and could not say. Kept on purpose:
            --                         "nobody answered" and "nobody looked" are
            --                         different, and one hard step must never
            --                         stall the queue.
            --
            -- evidence is what was on screen when they decided - so a verdict
            -- can be re-read later without re-deriving it.
            CREATE TABLE IF NOT EXISTS process_decision (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                workcell    TEXT NOT NULL,
                mes_step    TEXT NOT NULL,   -- byte-exact; trailing spaces matter
                answer      TEXT NOT NULL CHECK (answer IN
                                ('mapped', 'non_iedb', 'unknown')),
                iedb_alias  TEXT,            -- NULL unless answer='mapped'
                evidence    TEXT,
                decided_by  TEXT NOT NULL,   -- NTID, from the bearer token
                decided_on  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (workcell, mes_step)  -- one live answer; re-deciding replaces
            );

            CREATE INDEX IF NOT EXISTS idx_process_decision_wc
                ON process_decision (workcell);
        """)
    _migrate_saved_reports()


def _migrate_saved_reports() -> None:
    """Drop the old UNIQUE(module, report_type, owner_ntid, name) constraint.

    SQLite can't ALTER a constraint away, so the table is rebuilt. Rows are
    preserved. Idempotent and a no-op once migrated — it only fires while a
    UNIQUE auto-index is still present.
    """
    with get_conn() as conn:
        idx = conn.execute("PRAGMA index_list('saved_reports')").fetchall()
        if not any(r["unique"] and str(r["name"]).startswith("sqlite_autoindex") for r in idx):
            return                                    # already migrated
        conn.executescript("""
            PRAGMA foreign_keys=off;
            BEGIN;
            CREATE TABLE saved_reports_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                module      TEXT    NOT NULL,
                report_type TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                owner_ntid  TEXT    NOT NULL,
                owner_name  TEXT,
                owner_email TEXT,
                payload     TEXT    NOT NULL,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO saved_reports_new
                (id, module, report_type, name, owner_ntid, owner_name, owner_email,
                 payload, created_at, updated_at)
            SELECT id, module, report_type, name, owner_ntid, owner_name, owner_email,
                   payload, created_at, updated_at
            FROM saved_reports;
            DROP TABLE saved_reports;
            ALTER TABLE saved_reports_new RENAME TO saved_reports;
            CREATE INDEX IF NOT EXISTS idx_saved_reports_owner
                ON saved_reports (module, report_type, owner_ntid);
            COMMIT;
            PRAGMA foreign_keys=on;
        """)
