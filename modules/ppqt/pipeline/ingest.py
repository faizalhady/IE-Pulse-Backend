"""
modules/ppqt/pipeline/ingest.py
───────────────────────────────
Parse PPQT workbooks (EM-IE80-00003-B layout, LAMRES 8.0 as reference) from
data/raw/ppqt/*.xlsx into the PPQT marts.

Only VISIBLE sheets are read:

  PPQT sheets   G1 == 'PPQT Template'. One per Area x Period.
                  C7 workcell | title -> area code | G12 period date
                  row 13  station headers, H.. up to the last BOTTLENECK column
                  rows 14..footer-1  assemblies: A part no, C model, G demand,
                                     CT (sec) per station column
                  footer  per-station parameters, located BY LABEL because the
                          rows shift between sheets: crew hours, days, CO/day,
                          CO minutes, FPY, efficiency, equipment available.
                          The sheet's own 'Resources NEEDED' row is kept as the
                          golden value (modules/ppqt/compute.py checks against it).

  Exe Summaries the DL report. Bays x periods (type, DL/line, crew, NPI,
                available, plus the sheet's line requirement / DL required for
                the golden check) and the per-period scalars (weeks, volumes,
                actual DL, NVA DL, non-mfg DL, NVA target %).

Hidden sheets (MASTER FILE, UPH, SCR, Health Check, Summaries ...) are not read.

Run:  python -m modules.ppqt.pipeline.ingest
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import openpyxl
import pandas as pd

from modules.ppqt.config import (AREA_LABELS, BAY_AREA, OVERHEAD_AREAS,
                                 PPQT_MART, PPQT_RAW_DIR)

log = logging.getLogger(__name__)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ─── Cell helpers ─────────────────────────────────────────────────────────────

def _num(v) -> float:
    """Numeric cell or 0. Text ('x', '#REF!'), blanks and booleans are 0."""
    if v is None or isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return 0.0


def _txt(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _norm(v) -> str:
    return re.sub(r"\s+", " ", _txt(v)).lower()


def _period(v, title: str = "") -> str:
    """'YYYY-MM' from a date cell; falls back to a MON'YY / Qn'YY title suffix."""
    if isinstance(v, (datetime, date)):
        return f"{v.year:04d}-{v.month:02d}"
    m = re.search(r"([A-Za-z]{3,9})\s*'\s*(\d{2})\s*$", title)
    if m and m.group(1)[:3].lower() in _MONTHS:
        return f"20{m.group(2)}-{_MONTHS[m.group(1)[:3].lower()]:02d}"
    m = re.search(r"(Q[1-4])\s*'\s*(\d{2})\s*$", title, re.I)
    if m:
        return f"20{m.group(2)}-{m.group(1).upper()}"
    return _txt(v) or title.strip()


class Grid:
    """A worksheet materialised once; 1-based (row, col) access, label search."""

    def __init__(self, ws):
        self.rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
        self.nrows = len(self.rows)
        self.ncols = max((len(r) for r in self.rows), default=0)

    def at(self, r: int, c: int):
        if r < 1 or r > self.nrows:
            return None
        row = self.rows[r - 1]
        return row[c - 1] if 0 < c <= len(row) else None

    def find_row(self, prefix: str, cols=range(1, 9), start=1, end=None) -> int | None:
        """First row whose normalised text in any of `cols` starts with `prefix`."""
        end = end or self.nrows
        for r in range(start, end + 1):
            for c in cols:
                if _norm(self.at(r, c)).startswith(prefix):
                    return r
        return None


# ─── PPQT sheet ───────────────────────────────────────────────────────────────

_FOOTER_LABELS = {
    # key            label prefix (normalised)
    "footer":        "total qty, of changeovers",
    "dthrough":      "total demand by process",
    "crew1":         "crew1/shift1",
    "crew2":         "crew2/shift2",
    "crew3":         "crew3/shift3",
    "crew4":         "crew4/shift4",
    "days":          "days in the period of the demand",
    "co_per_day":    "qty. of changeovers per day=",
    "co_min":        "time for changeover (min",
    "fpy":           "fpy",
    "eff":           "efficiency",
    "need":          "resources needed",
    "eq":            "qty. equipment available",
}
_ERR = {"#REF!", "#DIV/0!", "#VALUE!", "#N/A", "#NAME?", "#NUM!", "#NULL!"}
_STATION_COL0 = 8   # column H
_HEADER_ROW = 13
_FIRST_ASM_ROW = 14


def _area_code(title: str, c8: str) -> str:
    m = re.search(r"\b(" + "|".join(AREA_LABELS) + r")\b", title, re.I)
    return m.group(1).upper() if m else (c8 or title).strip()


def parse_ppqt_sheet(ws, source: str) -> dict | None:
    g = Grid(ws)
    title = ws.title
    if _txt(g.at(1, 7)) != "PPQT Template":
        return None
    workcell = _txt(g.at(7, 3)).upper()
    area = _area_code(title, _txt(g.at(8, 3)))
    period = _period(g.at(12, 7), title)
    pdate = g.at(12, 7) if isinstance(g.at(12, 7), (datetime, date)) else None

    r0 = g.find_row(_FOOTER_LABELS["footer"], cols=range(2, 5), start=_FIRST_ASM_ROW)
    if not r0:
        log.warning(f"[{source}] {title}: footer not found - sheet skipped")
        return None
    rows = {k: g.find_row(p, cols=range(2, 9), start=r0, end=min(g.nrows, r0 + 60))
            for k, p in _FOOTER_LABELS.items()}
    missing = [k for k in ("days", "fpy", "eff", "need") if not rows[k]]
    if missing:
        log.warning(f"[{source}] {title}: footer rows missing {missing} - sheet skipped")
        return None

    # Station columns: H.. up to the last BOTTLENECK header. Blank headers are
    # spacer columns. Columns after the last BOTTLENECK (OLE, Lead Time) are
    # derived, not stations.
    headers = [(c, _txt(g.at(_HEADER_ROW, c))) for c in range(_STATION_COL0, g.ncols + 1)]
    bn_cols = [c for c, h in headers if h.upper() == "BOTTLENECK"]
    last = bn_cols[-1] if bn_cols else max((c for c, h in headers if h and h.upper() not in ("OLE", "LEADTIME", "LEAD TIME")), default=_STATION_COL0 - 1)

    stations: list[dict] = []
    group_no, group_cols = 1, []
    for c, h in headers:
        if c > last:
            break
        if not h:
            continue
        is_bn = h.upper() == "BOTTLENECK"
        group_cols.append((c, h, is_bn))
        if is_bn or c == last:
            members = [x for x in group_cols if not x[2]]
            label = next((_txt(g.at(12, cc)) for cc, _, _ in group_cols if _txt(g.at(12, cc))), "")
            if not label and members:
                label = members[0][1] if len(members) == 1 else f"{members[0][1]} - {members[-1][1]}"
            label = re.sub(r"\s+", " ", label)
            for seq, (cc, hh, bn) in enumerate(group_cols):
                stations.append({"col": cc, "station": re.sub(r"\s+", " ", hh), "is_bottleneck": bn,
                                 "line_group": label, "group_no": group_no})
            group_no, group_cols = group_no + 1, []

    # The 'Total Demand by process=' label is missing on some sheets; its
    # values always sit on the footer's first row.
    rows["dthrough"] = rows["dthrough"] or r0

    issues_by_col: dict[int, list[str]] = {}

    def fval(key, c):
        if not rows[key]:
            return 0.0
        v = g.at(rows[key], c)
        if isinstance(v, str) and v.strip() in _ERR:
            # A broken cell (#REF! days, ...) zeroes the sheet's own result. We
            # reproduce that and flag it, so the page can say "fix the workbook".
            issues_by_col.setdefault(c, []).append(f"{key}={v.strip()}")
        return _num(v)

    st_rows, seen = [], {}
    for seq, s in enumerate(stations):
        c = s["col"]
        name = s["station"]
        # A header can repeat across line groups (Wash, Mascot 1). Keep names
        # unique per sheet by suffixing the group number on repeats.
        if (name, s["is_bottleneck"]) in seen or name in seen:
            name = f"{name} [{s['group_no']}]"
        seen[name] = True
        s["key"] = name
        hours = sum(fval(k, c) for k in ("crew1", "crew2", "crew3", "crew4"))
        row = {
            "workcell": workcell, "area": area, "period": period,
            "station": name, "header": s["station"], "seq": seq,
            "line_group": s["line_group"], "group_no": s["group_no"],
            "is_bottleneck": s["is_bottleneck"],
            "hours_per_day": hours, "days": fval("days", c),
            "co_per_day": fval("co_per_day", c), "co_min": fval("co_min", c),
            "fpy": fval("fpy", c), "eff": fval("eff", c),
            "eq_avail": 0.0 if s["is_bottleneck"] else fval("eq", c),
            "sheet_need_allow": fval("need", c),
            "sheet_demand_through": fval("dthrough", c),
        }
        row["issues"] = "; ".join(issues_by_col.get(c, []))
        st_rows.append(row)

    asm_rows, ct_rows = [], []
    members = [s for s in stations if not s["is_bottleneck"]]
    for r in range(_FIRST_ASM_ROW, r0):
        a = g.at(r, 1)
        if a is None or a == 0 or _txt(str(a)) in ("", "0"):
            continue
        name = str(a).strip()
        demand = _num(g.at(r, 7))
        lead = 0.0
        for s in members:
            ct = _num(g.at(r, s["col"]))
            if ct > 0:
                lead += ct
                ct_rows.append({"workcell": workcell, "area": area, "period": period,
                                "sheet_row": r, "assembly": name, "station": s["key"], "ct_sec": ct})
        asm_rows.append({
            "workcell": workcell, "area": area, "period": period, "sheet_row": r,
            "assembly": name, "family": _txt(g.at(r, 2)), "model": _txt(g.at(r, 3)),
            "lot_size": _num(g.at(r, 4)), "changeovers": _num(g.at(r, 5)),
            "boards_per_panel": _num(g.at(r, 6)), "demand": demand, "lead_time_sec": lead,
        })

    log.info(f"[{source}] {title}: {workcell} / {area} / {period} - "
             f"{len(st_rows)} stations, {len(asm_rows)} assemblies, "
             f"{sum(1 for x in asm_rows if x['demand'] > 0)} with demand")
    return {"workcell": workcell, "area": area, "period": period, "period_date": pdate,
            "sheet": title, "stations": st_rows, "assemblies": asm_rows, "cycle_times": ct_rows}


# ─── Exe Summaries ────────────────────────────────────────────────────────────

def parse_exe_summaries(ws, workcell: str, source: str) -> tuple[list[dict], list[dict]]:
    g = Grid(ws)
    hdr = g.find_row("area", cols=[2], end=20)
    if not hdr:
        log.warning(f"[{source}] {ws.title}: header row not found")
        return [], []
    blocks = [c for c in range(1, g.ncols + 1) if _norm(g.at(hdr, c)).startswith("line")]
    if not blocks:
        log.warning(f"[{source}] {ws.title}: no period blocks found")
        return [], []

    periods = []
    for b in blocks:
        d = g.at(1, b)
        periods.append({
            "workcell": workcell, "period": _period(d),
            "period_date": d if isinstance(d, (datetime, date)) else None,
            "weeks": _num(g.at(2, b)), "pca_vol": _num(g.at(3, b)), "hla_vol": _num(g.at(4, b)),
            "actual_dl": 0.0, "nva_dl": 0.0, "non_mfg_dl": 0.0, "nva_target": 0.2,
            "_block": b,
        })

    bays, prev_area, r = [], "", hdr + 1
    while r <= g.nrows:
        if all(g.at(r, c) is None for c in range(1, 7)):
            break
        a = g.at(r, 1)
        area = _txt(a) if isinstance(a, str) else prev_area   # forward-fill typos (a bare number)
        prev_area = area or prev_area
        bay = _txt(g.at(r, 3))
        if bay:
            for seq, (p, b) in enumerate(zip(periods, blocks)):
                bays.append({
                    "workcell": workcell, "period": p["period"],
                    "area": area, "area_code": BAY_AREA.get(area),
                    "is_overhead": area in OVERHEAD_AREAS,
                    "bay": bay, "type": _txt(g.at(r, 4)), "seq": r,
                    "dl_per_line": _num(g.at(r, 5)), "crew": _num(g.at(r, 6)),
                    "npi": _num(g.at(r, b + 1)), "available": _num(g.at(r, b + 3)),
                    "sheet_line_req": _num(g.at(r, b)), "sheet_ttl_req": _num(g.at(r, b + 2)),
                    "sheet_variance": _num(g.at(r, b + 4)), "sheet_dl_required": _num(g.at(r, b + 5)),
                })
        r += 1

    # Scalars live under the DL Required column of each block: label at b+4, value at b+5.
    for p in periods:
        b = p.pop("_block")
        for rr in range(r, g.nrows + 1):
            label = _norm(g.at(rr, b + 4))
            val = _num(g.at(rr, b + 5))
            if not label:
                continue
            if "actual" in label and "dl" in label:
                p["actual_dl"] = val
            elif label == "nva":
                p["nva_dl"] = p["nva_dl"] or val
            elif label.startswith("non mfg"):
                p["non_mfg_dl"] = p["non_mfg_dl"] or val
            elif "nva target" in label:
                m = re.search(r"(\d+)\s*%", label)
                if m:
                    p["nva_target"] = int(m.group(1)) / 100

    log.info(f"[{source}] {ws.title}: {len(periods)} periods, {len(bays) // max(1, len(periods))} bays")
    return bays, periods


# ─── Workbook ─────────────────────────────────────────────────────────────────

def parse_workbook(path: Path) -> dict:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = {"stations": [], "assemblies": [], "cycle_times": [], "bays": [], "periods": [], "sheets": []}
    workcell, exe = "", None
    try:
        for ws in wb.worksheets:
            if ws.sheet_state != "visible":
                continue
            if ws.title.strip().lower().startswith("exe"):
                exe = ws
                continue
            res = parse_ppqt_sheet(ws, path.name)
            if not res:
                continue
            workcell = workcell or res["workcell"]
            for k in ("stations", "assemblies", "cycle_times"):
                out[k].extend(res[k])
            out["sheets"].append(res["sheet"])
        if exe is not None and workcell:
            bays, periods = parse_exe_summaries(exe, workcell, path.name)
            out["bays"], out["periods"] = bays, periods
            out["sheets"].append(exe.title)
    finally:
        wb.close()
    out["workcell"] = workcell
    return out


def run() -> bool:
    files = sorted(p for p in PPQT_RAW_DIR.glob("*.xlsx") if not p.name.startswith("~$"))
    if not files:
        log.warning(f"No workbooks in {PPQT_RAW_DIR}")
        return False

    frames = {k: [] for k in ("stations", "assemblies", "cycle_times", "bays", "periods")}
    books = []
    for f in files:
        log.info(f"Parsing {f.name} ({f.stat().st_size // 1024} KB)")
        try:
            res = parse_workbook(f)
        except Exception as e:          # one bad workbook must not sink the others
            log.exception(f"{f.name}: {e}")
            continue
        if not res["workcell"]:
            log.warning(f"{f.name}: no visible PPQT sheets - skipped")
            continue
        for k in frames:
            frames[k].extend(res[k])
        st = pd.DataFrame(res["stations"])
        books.append({
            "file": f.name, "workcell": res["workcell"],
            "areas": ",".join(sorted(st["area"].unique())) if len(st) else "",
            "periods": ",".join(sorted(st["period"].unique())) if len(st) else "",
            "sheets": len(res["sheets"]),
            "file_mtime": datetime.fromtimestamp(f.stat().st_mtime),
            "ingested_at": datetime.now(),
        })

    cols = {
        "stations": ["workcell", "area", "period", "station", "header", "seq", "line_group", "group_no",
                     "is_bottleneck", "hours_per_day", "days", "co_per_day", "co_min", "fpy", "eff",
                     "eq_avail", "sheet_need_allow", "sheet_demand_through", "issues"],
        "assemblies": ["workcell", "area", "period", "sheet_row", "assembly", "family", "model",
                       "lot_size", "changeovers", "boards_per_panel", "demand", "lead_time_sec"],
        "cycle_times": ["workcell", "area", "period", "sheet_row", "assembly", "station", "ct_sec"],
        "bays": ["workcell", "period", "area", "area_code", "is_overhead", "bay", "type", "seq",
                 "dl_per_line", "crew", "npi", "available", "sheet_line_req", "sheet_ttl_req",
                 "sheet_variance", "sheet_dl_required"],
        "periods": ["workcell", "period", "period_date", "weeks", "pca_vol", "hla_vol",
                    "actual_dl", "nva_dl", "non_mfg_dl", "nva_target"],
    }
    for k, c in cols.items():
        df = pd.DataFrame(frames[k], columns=c)
        df.to_parquet(PPQT_MART[k], index=False)
        log.info(f"  {PPQT_MART[k].name:<22} {len(df):>8,} rows")
    pd.DataFrame(books, columns=["file", "workcell", "areas", "periods", "sheets", "file_mtime", "ingested_at"]) \
        .to_parquet(PPQT_MART["workbooks"], index=False)
    return bool(books)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(0 if run() else 1)
