"""
modules/ole/smh_store.py
────────────────────────
Read/write access to the `smh` table — the source of truth for Standard Man
Hours per unit, and therefore the numerator of every OLE number.

Before this existed, SMH lived in ten HTML-disguised .xls files that the
pipeline re-parsed on every run. Editing SMH from the UI was impossible: the
next refresh overwrote whatever the UI had written. The .xls files are still
accepted as a bulk input (`upsert_many` from the parser), but they are now an
import format, not the store.

Table + audit schema live in core/database.py.

Every mutation writes an audit row. That is not gold-plating — an SMH edit
silently moves the plant's headline percentage for every historical week, so
"who changed this, and what was it before" has to outlive the row itself.
"""

import logging
import sqlite3
from typing import Iterable, Optional

import pandas as pd

from core.database import get_conn

log = logging.getLogger(__name__)


class SmhError(Exception):
    """Business-rule violation. Routers translate this to a 4xx."""


# Below this, an SMH is a data-entry error, not a real standard.
#
# 0.0001 hr = 0.36 seconds to build one unit. Nothing is built that fast, and
# the real data agrees: there is a clean cliff at this value — 51 rows sit
# below it, the next 1,375 sit between it and 0.01. The offenders are almost
# all LAM RESEARCH values of the form 4.761905e-N (1/21 with the decimal place
# slipped), which is a units mistake rather than a measurement.
#
# These are worse than a missing SMH. A missing one is visible on the SMH page
# and reads as "needs filling"; a value of 4.8e-10 looks populated while
# earning nothing, so the workcell's OLE is quietly wrong with no flag.
MIN_SMH = 1e-4


# ─── Validation ───────────────────────────────────────────────────────────────

def _clean_value(v) -> float:
    """SMH must be a positive number.

    Zero is rejected as hard as a negative: compute.py's LEFT JOIN treats
    `smh_value = 0` and "no row at all" identically (both land in
    qty_missing_smh), so storing 0 would be an invisible way to zero out a
    workcell's earned hours. If the value isn't known, the row shouldn't exist.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise SmhError(f"SMH value must be a number, got {v!r}")
    if f != f or f in (float("inf"), float("-inf")):     # NaN / inf
        raise SmhError("SMH value must be a finite number")
    if f <= 0:
        raise SmhError("SMH value must be greater than 0. To record 'not known', delete the row instead.")
    if f < MIN_SMH:
        raise SmhError(
            f"SMH value {f:g} is below the {MIN_SMH:g} minimum ({MIN_SMH * 3600:.2g} seconds per unit) "
            "— that is a decimal-place error, not a standard. Fix the value or delete the row."
        )
    return f


def _clean_key(workcell: str, assembly: str) -> tuple[str, str]:
    wc = (workcell or "").strip()
    asm = (assembly or "").strip().rstrip("/")   # matches _parse_smh_xls's trailing-slash strip
    if not wc:
        raise SmhError("workcell is required")
    if not asm:
        raise SmhError("assembly is required")
    return wc, asm


def _audit(conn: sqlite3.Connection, workcell: str, assembly: str, action: str,
           old_value: Optional[float], new_value: Optional[float], by: Optional[str]) -> None:
    conn.execute(
        "INSERT INTO smh_audit (workcell, assembly, action, old_value, new_value, changed_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (workcell, assembly, action, old_value, new_value, by),
    )


def _current(conn: sqlite3.Connection, workcell: str, assembly: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM smh WHERE workcell = ? AND assembly = ?", (workcell, assembly)
    ).fetchone()


# ─── Reads ────────────────────────────────────────────────────────────────────

def list_smh(workcell: Optional[str] = None, search: Optional[str] = None,
             limit: int = 5000, offset: int = 0) -> list[dict]:
    clauses, params = [], []
    if workcell:
        clauses.append("workcell = ?")
        params.append(workcell.strip())
    if search:
        clauses.append("assembly LIKE ?")
        params.append(f"%{search.strip()}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM smh {where} ORDER BY workcell, assembly LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def count_smh(workcell: Optional[str] = None) -> int:
    with get_conn() as conn:
        if workcell:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM smh WHERE workcell = ?", (workcell.strip(),)
            ).fetchone()["n"]
        return conn.execute("SELECT COUNT(*) AS n FROM smh").fetchone()["n"]


def load_lookup() -> pd.DataFrame:
    """The whole table, shaped exactly like the old _parse_smh_xls output.

    ingest_smh() writes this straight to smh_lookup.parquet, so the column set
    here IS the mart schema. compute.py joins on (workcell, assembly) and reads
    smh_value / scan_stage / stage_label — changing these names breaks it.
    """
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT workcell, assembly, smh_value, scan_stage, stage_label, plant, "
            "       updated_at AS last_updated "
            "FROM smh ORDER BY workcell, assembly",
            conn,
        )
    if not df.empty:
        df["last_updated"] = pd.to_datetime(df["last_updated"], errors="coerce")
    return df


def history(workcell: Optional[str] = None, assembly: Optional[str] = None,
            limit: int = 200) -> list[dict]:
    clauses, params = [], []
    if workcell:
        clauses.append("workcell = ?")
        params.append(workcell.strip())
    if assembly:
        clauses.append("assembly = ?")
        params.append(assembly.strip())
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM smh_audit {where} ORDER BY changed_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Writes ───────────────────────────────────────────────────────────────────

def create(workcell: str, assembly: str, smh_value, by: Optional[str],
           scan_stage: Optional[str] = None, stage_label: Optional[str] = None,
           plant: Optional[str] = None) -> dict:
    wc, asm = _clean_key(workcell, assembly)
    val = _clean_value(smh_value)
    with get_conn() as conn:
        if _current(conn, wc, asm):
            raise SmhError(f"{asm} already exists for {wc} — edit it instead of creating a duplicate")
        conn.execute(
            "INSERT INTO smh (workcell, assembly, smh_value, scan_stage, stage_label, plant, "
            "                 source, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'manual', ?, datetime('now'))",
            (wc, asm, val, scan_stage, stage_label, plant, by),
        )
        _audit(conn, wc, asm, "create", None, val, by)
        row = _current(conn, wc, asm)
    log.info("SMH create: %s / %s = %s by %s", wc, asm, val, by)
    return dict(row)


def update(workcell: str, assembly: str, smh_value, by: Optional[str]) -> dict:
    wc, asm = _clean_key(workcell, assembly)
    val = _clean_value(smh_value)
    with get_conn() as conn:
        existing = _current(conn, wc, asm)
        if not existing:
            raise SmhError(f"No SMH row for {wc} / {asm}")
        old = existing["smh_value"]
        conn.execute(
            "UPDATE smh SET smh_value = ?, source = 'manual', updated_by = ?, "
            "               updated_at = datetime('now') "
            "WHERE workcell = ? AND assembly = ?",
            (val, by, wc, asm),
        )
        _audit(conn, wc, asm, "update", old, val, by)
        row = _current(conn, wc, asm)
    log.info("SMH update: %s / %s  %s -> %s by %s", wc, asm, old, val, by)
    return dict(row)


def delete(workcell: str, assembly: str, by: Optional[str]) -> None:
    wc, asm = _clean_key(workcell, assembly)
    with get_conn() as conn:
        existing = _current(conn, wc, asm)
        if not existing:
            raise SmhError(f"No SMH row for {wc} / {asm}")
        conn.execute("DELETE FROM smh WHERE workcell = ? AND assembly = ?", (wc, asm))
        _audit(conn, wc, asm, "delete", existing["smh_value"], None, by)
    log.info("SMH delete: %s / %s (was %s) by %s", wc, asm, existing["smh_value"], by)


def upsert_many(rows: Iterable[dict], by: Optional[str], source: str = "xls") -> dict:
    """Bulk insert/update — the .xls import path and the one-time migration.

    Rows with a non-positive SMH are SKIPPED, not stored as 0. The .xls files
    are full of blank cells that the parser turns into 0.0; importing those
    would write rows that look present but earn nothing, which is worse than an
    honest gap. The skipped count is returned so the caller can report it.
    """
    created = updated = skipped = 0
    with get_conn() as conn:
        for r in rows:
            try:
                wc, asm = _clean_key(r.get("workcell"), r.get("assembly"))
                val = _clean_value(r.get("smh_value"))
            except SmhError:
                skipped += 1
                continue

            existing = _current(conn, wc, asm)
            old = existing["smh_value"] if existing else None
            if existing and old == val:
                continue                                  # no-op, no audit noise

            conn.execute(
                "INSERT INTO smh (workcell, assembly, smh_value, scan_stage, stage_label, "
                "                 plant, source, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT (workcell, assembly) DO UPDATE SET "
                "  smh_value = excluded.smh_value, "
                "  scan_stage = COALESCE(excluded.scan_stage, smh.scan_stage), "
                "  stage_label = COALESCE(excluded.stage_label, smh.stage_label), "
                "  plant = COALESCE(excluded.plant, smh.plant), "
                "  source = excluded.source, "
                "  updated_by = excluded.updated_by, "
                "  updated_at = datetime('now')",
                (wc, asm, val, r.get("scan_stage"), r.get("stage_label"),
                 r.get("plant"), source, by),
            )
            _audit(conn, wc, asm, "import" if not existing else "update", old, val, by)
            if existing:
                updated += 1
            else:
                created += 1

    log.info("SMH bulk upsert (%s): %d created, %d updated, %d skipped",
             source, created, updated, skipped)
    return {"created": created, "updated": updated, "skipped": skipped}


def purge_below_minimum(by: Optional[str] = "cleanup") -> list[dict]:
    """Delete rows whose SMH is below MIN_SMH, returning what was removed.

    Only needed for rows imported before the floor existed. Each delete is
    audited, so the old values are recoverable from smh_audit.
    """
    with get_conn() as conn:
        doomed = [dict(r) for r in conn.execute(
            "SELECT workcell, assembly, smh_value FROM smh WHERE smh_value < ? "
            "ORDER BY workcell, smh_value", (MIN_SMH,)
        ).fetchall()]
        for r in doomed:
            conn.execute("DELETE FROM smh WHERE workcell = ? AND assembly = ?",
                         (r["workcell"], r["assembly"]))
            _audit(conn, r["workcell"], r["assembly"], "delete", r["smh_value"], None, by)
    log.info("Purged %d SMH rows below the %g minimum", len(doomed), MIN_SMH)
    return doomed
