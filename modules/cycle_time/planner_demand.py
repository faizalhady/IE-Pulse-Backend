"""
planner_demand.py — Planner-demand ingestion (TEMPORARY, partial-coverage source).

Parses the planners' demand Excels under data/demand/CH/Original Demand/ into one
tidy mart:  data/mart/demand/planner_demand.parquet

    columns: workcell, model, period_start(date), period_type('week'|'month'),
             qty(int), source(str), as_of(date)

Design notes
------------
- Mixed granularity is kept NATIVE (period_type). Reporting sums qty over a common
  forward window, so weekly and monthly sources stay comparable without faking weeks.
- This is a SEPARATE source from the MES buildplan projection — different origin,
  covers only ~13 workcells (Plant-1 medical + a few). Keep it isolated.
- Templates vary per file → a MANIFEST holds each file's shape + params. Add a file
  = one manifest entry, not new code.
- Model→assembly join uses canon() (strip ZZ prefix / EV#/RMA suffix / revision).
  build_report() emits a near-miss QA list so same-part-different-spelling gets
  flagged instead of silently read as "no data".
"""

from __future__ import annotations
import re
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent.parent.parent          # IE-Pulse-Backend/
DEMAND_DIR = BASE / "data" / "demand" / "CH" / "Original Demand"
ROOT_DIR = BASE / "data" / "demand"                 # Arista lives here, not in CH/
DIRS = {"od": DEMAND_DIR, "root": ROOT_DIR}
OUT_PARQUET = BASE / "data" / "mart" / "demand" / "planner_demand.parquet"

_DATE_HDR = re.compile(r"^\d{2}-\d{2}-\d{2}$")       # MM-DD-YY weekly capmodel header

# Profit-center code → cycle-time workcell (config) name. '(skip)' = extract but drop.
PC2WC = {
    "0301ASP": "ASP", "0301BD": "BD", "0301BECKMN": "BECKMAN COULTER",
    "0301MASIMO": "Masimo", "0301THERMO": "TMO", "0301ILLUMI": "ILLUMINA",
    "0301LAMRES": "LAMRESEARCH", "0301LTX": "LTX", "0301UTAS": "UTAS",
    "0301WABTEC": "WABTEC", "0301KSIGHT": "KEYSIGHT", "0301VERIGY": "(skip)",
}


def canon(s: str) -> str:
    """Normalise a part/assembly name for joining: alphanumeric-only, drop a leading
    ZZ, an EV#/RMA engineering suffix, and a trailing revision letter block."""
    n = re.sub(r"[^A-Z0-9]", "", str(s).upper())
    n = re.sub(r"^ZZ", "", n)
    n = re.sub(r"(EV\d*|H?RMA|CRMA|BRMA)$", "", n)
    n = re.sub(r"[A-Z]+$", "", n)
    return n


# ─── Shape parsers — each returns rows of (workcell, model, period_start, period_type, qty) ──

def _parse_capmodel(path: Path, sheet: str, part_col: int, label_col: int) -> list[tuple]:
    """Wide weekly capmodel/xtab grid. Profit-center=col3, keep label=='Qty' rows,
    melt the MM-DD-YY weekly columns. Skips 'Past'/'Sum' (non-date) columns."""
    raw = pd.read_excel(path, sheet_name=sheet, header=None)
    hdr = max(range(min(6, len(raw))),
              key=lambda r: sum(bool(_DATE_HDR.match(str(v))) for v in raw.iloc[r]))
    datecols = {c: pd.to_datetime(str(raw.iloc[hdr, c]), format="%m-%d-%y").date()
                for c in raw.columns if _DATE_HDR.match(str(raw.iloc[hdr, c]))}
    body = raw.iloc[hdr + 1:]
    q = body[body[label_col].astype(str).str.strip().str.lower() == "qty"]
    rows = []
    for _, r in q.iterrows():
        wc = PC2WC.get(str(r[3]).strip())
        part = str(r[part_col]).strip()
        if not wc or wc == "(skip)" or part in ("nan", ""):
            continue
        for c, d in datecols.items():
            v = pd.to_numeric(r[c], errors="coerce")
            if pd.notna(v) and v > 0:
                rows.append((wc, part, d, "week", int(v)))
    return rows


def _parse_order(path: Path, sheet: str, workcell: str,
                 model_col: int, qty_col: int, date_col: int) -> list[tuple]:
    """Order-level long: one row per dated order. Bucket each order to its week."""
    df = pd.read_excel(path, sheet_name=sheet, header=0)
    df = df.rename(columns={df.columns[model_col]: "m",
                            df.columns[qty_col]: "q", df.columns[date_col]: "d"})
    df = df[pd.to_numeric(df["q"], errors="coerce").notna()]
    df["d"] = pd.to_datetime(df["d"], errors="coerce")
    df = df[df["d"].notna()]
    rows = []
    for _, r in df.iterrows():
        wk = (r["d"] - pd.Timedelta(days=r["d"].weekday())).date()   # week-start (Mon)
        q = int(pd.to_numeric(r["q"]))
        if q > 0 and str(r["m"]).strip() not in ("nan", ""):
            rows.append((workcell, str(r["m"]).strip(), wk, "week", q))
    return rows


def _parse_long(path: Path, sheet: str, workcell: str,
                model_col: str, qty_col: str, date_col: str) -> list[tuple]:
    """Tidy long forecast (LAMMEC Output): named Part/Quantity/Date columns → weekly."""
    df = pd.read_excel(path, sheet_name=sheet, header=0)
    df["_d"] = pd.to_datetime(df[date_col], errors="coerce")
    df = df[df["_d"].notna()]
    rows = []
    for _, r in df.iterrows():
        q = pd.to_numeric(r[qty_col], errors="coerce")
        m = str(r[model_col]).strip()
        if pd.notna(q) and q > 0 and m not in ("nan", ""):
            wk = (r["_d"] - pd.Timedelta(days=r["_d"].weekday())).date()
            rows.append((workcell, m, wk, "week", int(q)))
    return rows


def _parse_weekly_wide(path: Path, sheet: str, workcell: str, model_col: int = 0) -> list[tuple]:
    """Single-workcell weekly grid with real datetime column headers (Arista)."""
    raw = pd.read_excel(path, sheet_name=sheet, header=None)
    hdr = max(range(min(3, len(raw))),
              key=lambda r: sum(1 for v in raw.iloc[r]
                                if str(v).startswith("20") and pd.notna(pd.to_datetime(str(v), errors="coerce"))))
    datecols = {}
    for c in raw.columns:
        d = pd.to_datetime(str(raw.iloc[hdr, c]), errors="coerce")
        if pd.notna(d) and 2025 <= d.year <= 2030:
            datecols[c] = d.date()
    rows = []
    for _, r in raw.iloc[hdr + 1:].iterrows():
        m = str(r[model_col]).strip()
        if m in ("nan", ""):
            continue
        for c, d in datecols.items():
            v = pd.to_numeric(r[c], errors="coerce")
            if pd.notna(v) and v > 0:
                rows.append((workcell, m, d, "week", int(v)))
    return rows


def _parse_monthly(path: Path, sheet: str, workcell: str, header_row: int, model_col: int,
                   month_spec, drop_models: tuple = (), rollover: bool = False) -> list[tuple]:
    """Single-workcell monthly-wide grid. month_spec='auto' parses the header cells as
    dates (rollover=True fixes the 'all stamped 2026 but sequence rolls into 2027' glitch);
    or pass an explicit {col: 'YYYY-MM-DD'} for files with string/typo month labels."""
    from datetime import date as _date
    raw = pd.read_excel(path, sheet_name=sheet, header=None)
    if month_spec == "auto":
        hdr = raw.iloc[header_row]
        cols = sorted((c, pd.to_datetime(str(hdr[c]), errors="coerce")) for c in raw.columns
                      if pd.notna(pd.to_datetime(str(hdr[c]), errors="coerce"))
                      and 2025 <= pd.to_datetime(str(hdr[c]), errors="coerce").year <= 2030)
        months, off, prev = {}, 0, 0
        for c, d in cols:
            if rollover and d.month < prev:
                off += 1
            prev = d.month
            months[c] = _date(d.year + off, d.month, 1)
    else:
        months = {c: pd.to_datetime(v).date().replace(day=1) for c, v in month_spec.items()}
    rows = []
    for _, r in raw.iloc[header_row + 1:].iterrows():
        m = str(r[model_col]).strip()
        if m in ("nan", "") or m in drop_models:
            continue
        for c, d in months.items():
            v = pd.to_numeric(r[c], errors="coerce")
            if pd.notna(v) and v > 0:
                rows.append((workcell, m, d, "month", int(v)))
    return rows


# ─── Manifest — kept, deduped sources only (one per workcell). ──────────────────
MANIFEST = [
    {"file": "Danaher, TMO, Fortive, Masimo & BD SMT Capmodel 062326.xlsx",
     "shape": "capmodel", "sheet": "MPS (PO)", "part_col": 10, "label_col": 11, "as_of": "2026-06-23"},
    {"file": "AOP1 SMT Capmodel 062426 rev00.xlsx",
     "shape": "capmodel", "sheet": "MPS (PO)", "part_col": 10, "label_col": 11, "as_of": "2026-06-24"},
    {"file": "Schedule Xtab_SCR_260622 Keysight TOP Model AOP.xlsx",
     "shape": "capmodel", "sheet": "Schedule Xtab", "part_col": 5, "label_col": 6, "as_of": "2026-06-22"},
    {"file": "PLAN ORDER-060326_ppqt date 5 June26 Resmed.xlsx",
     "shape": "order", "sheet": "Sheet1", "workcell": "ResMed",
     "model_col": 0, "qty_col": 2, "date_col": 3, "as_of": "2026-06-03"},
    {"file": "LAMMECH Forecast_LBR.xlsx",
     "shape": "long", "sheet": "Output", "workcell": "LAMMEC",
     "model_col": "Part", "qty_col": "Quantity", "date_col": "Date", "as_of": "2026-06-08"},
    {"file": "ARISTA.xlsx", "base": "root",
     "shape": "weekly_wide", "sheet": "Sheet2", "workcell": "ARISTANETWORKS",
     "model_col": 0, "as_of": "2026-06-29"},
    {"file": "Demand Plan Jun26 - Advantest.xlsx",
     "shape": "monthly", "sheet": "Sheet1", "workcell": "ADVANTEST",
     "header_row": 1, "model_col": 0, "month_spec": "auto", "drop_models": ["Total System"], "as_of": "2026-06-01"},
    {"file": "Medtronic CTB.xlsx",
     "shape": "monthly", "sheet": "Medtronic CTB", "workcell": "Medtronic",
     "header_row": 0, "model_col": 0, "month_spec": "auto", "rollover": True, "as_of": "2026-06-01"},
    {"file": "Micron CTB.xlsx",
     "shape": "monthly", "sheet": "Micron CTB", "workcell": "MICRON SIG",
     "header_row": 0, "model_col": 2, "month_spec": {3: "2026-07-01", 4: "2026-08-01", 5: "2026-09-01"}, "as_of": "2026-06-01"},
    {"file": "COHU HLA - CTB (July_Aug_Sep).xlsx",
     "shape": "monthly", "sheet": "HLA (Sub Assy) - CTB", "workcell": "Cohu",
     "header_row": 2, "model_col": 2, "month_spec": {3: "2026-07-01", 4: "2026-08-01", 5: "2026-09-01"}, "as_of": "2026-06-01"},
    {"file": "COHU HLA - CTB (July_Aug_Sep).xlsx",
     "shape": "monthly", "sheet": "HLA (Base system) - CTB", "workcell": "Cohu",
     "header_row": 2, "model_col": 2, "month_spec": {3: "2026-07-01", 4: "2026-08-01", 5: "2026-09-01"}, "as_of": "2026-06-01"},
]


def build_planner_demand() -> int:
    """Parse every manifest source → write planner_demand.parquet. Returns row count."""
    all_rows = []
    for m in MANIFEST:
        path = DIRS[m.get("base", "od")] / m["file"]
        if m["shape"] == "capmodel":
            rows = _parse_capmodel(path, m["sheet"], m["part_col"], m["label_col"])
        elif m["shape"] == "order":
            rows = _parse_order(path, m["sheet"], m["workcell"], m["model_col"], m["qty_col"], m["date_col"])
        elif m["shape"] == "long":
            rows = _parse_long(path, m["sheet"], m["workcell"], m["model_col"], m["qty_col"], m["date_col"])
        elif m["shape"] == "weekly_wide":
            rows = _parse_weekly_wide(path, m["sheet"], m["workcell"], m.get("model_col", 0))
        elif m["shape"] == "monthly":
            rows = _parse_monthly(path, m["sheet"], m["workcell"], m["header_row"], m["model_col"],
                                  m["month_spec"], tuple(m.get("drop_models", ())), m.get("rollover", False))
        else:
            continue
        src = m["file"].split(" SMT")[0].split(".xlsx")[0][:40]
        all_rows += [(*r, src, m["as_of"]) for r in rows]

    df = pd.DataFrame(all_rows, columns=["workcell", "model", "period_start", "period_type", "qty", "source", "as_of"])
    # collapse dup (workcell, model, period) — sum qty (e.g. multiple orders same week)
    df = df.groupby(["workcell", "model", "period_start", "period_type", "source", "as_of"],
                    as_index=False)["qty"].sum()
    OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_PARQUET, index=False)
    return len(df)


PLANNER_WEEKS = 13   # forward window summed into the Incompletion Report demand figure
EBUILD_MART = BASE / "data" / "mart" / "ebuild"
PLANNER_RUNNERS_PARQUET = EBUILD_MART / "planner_runners.parquet"


def build_planner_runners_mart(weeks: int = PLANNER_WEEKS) -> int:
    """Collapse planner_demand → the plant-runners schema so the Incompletion Report
    renders it as a 3rd toggle. Sums qty over [start-of-this-month, today+weeks] per
    (workcell, model); joins plant (customer_plant) and has_data (assembly_summary,
    canon-normalised). Cohu (non-cycle-time) drops out at the plant join."""
    from datetime import date, timedelta
    from modules.cycle_time.config import CT_MART

    dem = pd.read_parquet(OUT_PARQUET)
    dem["period_start"] = pd.to_datetime(dem["period_start"]).dt.date
    today = date.today()
    win_start, win_end = today.replace(day=1), today + timedelta(weeks=weeks)
    w = dem[(dem["period_start"] >= win_start) & (dem["period_start"] <= win_end)]

    g = (w.groupby(["workcell", "model"])
           .agg(units=("qty", "sum"), jobs=("period_start", "nunique"),
                first_start=("period_start", "min"), planned_finish=("period_start", "max"))
           .reset_index().rename(columns={"workcell": "customer", "model": "assembly"}))
    g["units"] = g["units"].astype(int)

    # plant per workcell (from the MES customer_plant mart; unmatched → NaN, shown in Overall only)
    cp = pd.read_parquet(EBUILD_MART / "customer_plant.parquet")
    pmap = {str(c).upper(): pl for c, pl in zip(cp["customer"], cp["plant"])}
    g["plant"] = g["customer"].str.upper().map(pmap)

    # has_data via canon() vs the cycle-time assembly set
    asum = pd.read_parquet(CT_MART["assembly_summary"], columns=["customer", "assembly"])
    lut: dict = {}
    for c, a in zip(asum["customer"], asum["assembly"]):
        lut.setdefault(str(c).upper(), set()).add(canon(a))
    g["has_data"] = [canon(a) in lut.get(str(c).upper(), set()) for c, a in zip(g["customer"], g["assembly"])]

    g["last_completed"] = pd.NaT
    g["first_start"] = pd.to_datetime(g["first_start"])
    g["planned_finish"] = pd.to_datetime(g["planned_finish"])
    cols = ["plant", "customer", "assembly", "units", "jobs", "last_completed", "first_start", "planned_finish", "has_data"]
    g = g[cols]
    PLANNER_RUNNERS_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    g.to_parquet(PLANNER_RUNNERS_PARQUET, index=False)
    return len(g)


def build_report() -> None:
    """Print per-workcell coverage + emit a near-miss QA list (unmatched demand
    models vs closest cycle-time assembly). Run after build_planner_demand()."""
    import difflib
    from modules.cycle_time.config import CT_MART

    dem = pd.read_parquet(OUT_PARQUET)
    asum = pd.read_parquet(CT_MART["assembly_summary"], columns=["customer", "assembly"])

    print(f"\nplanner_demand.parquet: {len(dem)} rows, {dem['workcell'].nunique()} workcells")
    print(f"{'workcell':<16}{'models':>7}{'weeks':>7}{'match%':>8}")
    near_miss = []
    for wc, g in dem.groupby("workcell"):
        models = g["model"].unique()
        ct = asum[asum.customer.str.upper() == wc.upper()]["assembly"].astype(str)
        ctset = {canon(a) for a in ct}
        ctmap = {canon(a): a for a in ct}
        matched = [m for m in models if canon(m) in ctset]
        span = g["period_start"].nunique()
        print(f"{wc:<16}{len(models):>7}{span:>7}{100*len(matched)//max(len(models),1):>7}%")
        for m in models:
            if canon(m) not in ctset:
                near = difflib.get_close_matches(canon(m), list(ctmap), n=1, cutoff=0.7)
                if near:  # close but not matched → likely a spelling rule we're missing
                    near_miss.append((wc, m, ctmap[near[0]]))
    if near_miss:
        print(f"\nNEAR-MISS QA — {len(near_miss)} unmatched-but-close (review for new spelling rules):")
        for wc, m, a in near_miss[:20]:
            print(f"  {wc:<14} {m:<24} ~ {a}")


if __name__ == "__main__":
    n = build_planner_demand()
    print(f"wrote {n} rows -> {OUT_PARQUET}")
    r = build_planner_runners_mart()
    print(f"wrote {r} planner-runner rows -> {PLANNER_RUNNERS_PARQUET}")
    build_report()
