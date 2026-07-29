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
            CREATE TABLE IF NOT EXISTS saved_reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                module      TEXT    NOT NULL,   -- 'ole' | 'cycle_time' | ...
                report_type TEXT    NOT NULL,   -- '4q'
                name        TEXT    NOT NULL,   -- user-chosen save name
                owner_ntid  TEXT    NOT NULL,   -- from RetrieveUserInfoNoParam
                owner_name  TEXT,
                owner_email TEXT,
                payload     TEXT    NOT NULL,   -- JSON; for 4q = the Q3 plan only
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE (module, report_type, owner_ntid, name)
            );

            CREATE INDEX IF NOT EXISTS idx_saved_reports_owner
                ON saved_reports (module, report_type, owner_ntid);
        """)
