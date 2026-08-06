"""
diagnose_shift.py
─────────────────
Targeted diagnostic for a single (workcell, date, shift) cell.
Pulls the same numbers from every pipeline layer and prints them
side-by-side so we can see exactly which layer changes the value.

Usage:
  python diagnose_shift.py <WORKCELL> <YYYY-MM-DD> <SHIFT>

Example:
  python diagnose_shift.py "ARISTA NETWORKS" 2026-05-05 1
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from modules.ole.config import (
    NETWORK_PATH, RAWDATA_PATH, MART,
    PRODUCTION_PREFIX, PAID_HOURS_RAW_PREFIX,
    WORKCELL_CONFIG, DATE_FROM,
)
from modules.ole.pipeline.ingest import _WORKCELL_MAP, _normalise_workcell


def banner(title):
    print()
    print("-" * 70)
    print(f"  {title}")
    print("-" * 70)


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    target_wc    = sys.argv[1]
    target_date  = sys.argv[2]
    target_shift = int(sys.argv[3])

    print(f"\nDiagnosing: workcell={target_wc!r}  date={target_date}  shift={target_shift}\n")

    cfg = WORKCELL_CONFIG.get(target_wc)
    if not cfg:
        print(f"!  '{target_wc}' is NOT in WORKCELL_CONFIG. Active workcells:")
        print(f"   {list(WORKCELL_CONFIG.keys())}")
        sys.exit(1)

    print(f"Config: scan_stage={cfg['scan_stage']!r}  plant={cfg['plant']!r}  smh_col={cfg['smh_col']!r}")

    # Find all raw paid hours and production CSVs matching the target date
    target_dt = pd.Timestamp(target_date)
    date_str  = target_dt.strftime("%Y%m%d")

    # ─── LAYER 1: RAW CSV (unfiltered) ────────────────────────────────────────
    banner(f"LAYER 1 — Raw CSV (no filters)")

    # PAID HOURS RAW
    raw_dirs = [RAWDATA_PATH]
    paid_files = []
    for d in raw_dirs:
        try:
            paid_files.extend(sorted(d.glob(f"{PAID_HOURS_RAW_PREFIX}*.csv")))
        except Exception as e:
            print(f"  Cannot access {d}: {e}")

    paid_raw = []
    for f in paid_files:
        try:
            try:
                df = pd.read_csv(f, dtype=str, encoding='utf-8')
            except UnicodeDecodeError:
                df = pd.read_csv(f, dtype=str, encoding='windows-1252')
            paid_raw.append(df)
        except Exception as e:
            print(f"  Failed to read {f.name}: {e}")

    if paid_raw:
        ph = pd.concat(paid_raw, ignore_index=True)
        ph.columns = [c.strip() for c in ph.columns]
        ph["Startdate"] = pd.to_datetime(ph["Startdate"], errors="coerce")
        ph["Shift"]     = pd.to_numeric(ph["Shift"], errors="coerce").fillna(0).astype(int)
        ph["TPHDirect"] = pd.to_numeric(ph["TPHDirect"], errors="coerce").fillna(0.0)

        # Variants that could normalise to target_wc
        variant_names = [k for k, v in _WORKCELL_MAP.items() if v == target_wc]
        print(f"  Workcell raw-name variants normalising to {target_wc!r}: {variant_names}")

        ph_target = ph[
            ph["WorkCell"].isin(variant_names)
            & (ph["Startdate"] == target_dt)
            & (ph["Shift"] == target_shift)
        ]
        print(f"  Paid hours rows in raw CSV: {len(ph_target)}")
        print(f"    SUM(TPHDirect) = {ph_target['TPHDirect'].sum():.2f}")
        if len(ph_target):
            print(f"    Value1 distribution: {ph_target['Value1'].fillna('').value_counts().to_dict()}")
            if "SubWorkCell" in ph_target.columns:
                print(f"    SubWorkCell distribution: {ph_target['SubWorkCell'].value_counts().to_dict()}")
    else:
        print("  No raw paid hours CSVs loaded.")

    # PRODUCTION
    prod_dirs = [NETWORK_PATH, RAWDATA_PATH]
    prod_files = []
    seen = set()
    for d in prod_dirs:
        try:
            for f in sorted(d.glob(f"{PRODUCTION_PREFIX}*.csv")):
                if f.name not in seen:
                    seen.add(f.name)
                    prod_files.append(f)
        except Exception as e:
            print(f"  Cannot access {d}: {e}")

    prod_raw = []
    for f in prod_files:
        try:
            df = pd.read_csv(f, dtype=str)
            prod_raw.append(df)
        except Exception as e:
            print(f"  Failed to read {f.name}: {e}")

    if prod_raw:
        pr = pd.concat(prod_raw, ignore_index=True)
        pr.columns = [c.strip() for c in pr.columns]
        pr["StartDate"] = pd.to_datetime(pr["StartDate"], errors="coerce")
        pr["Shift"]     = pd.to_numeric(pr["Shift"], errors="coerce").fillna(0).astype(int)
        pr["Qty"]       = pd.to_numeric(pr["Qty"], errors="coerce").fillna(0).astype(int)

        variant_names = [k for k, v in _WORKCELL_MAP.items() if v == target_wc]

        pr_target = pr[
            pr["Workcell"].isin(variant_names)
            & (pr["StartDate"] == target_dt)
            & (pr["Shift"] == target_shift)
        ]
        print(f"  Production rows in raw CSV: {len(pr_target)}")
        print(f"    SUM(Qty) = {pr_target['Qty'].sum()}")
        if len(pr_target):
            print(f"    SubWorkcell distribution: {pr_target['SubWorkcell'].value_counts().to_dict()}")
            print(f"    SubWorkcell EXPECTED by config (will be kept): {cfg['scan_stage']!r}")
            kept = pr_target[pr_target["SubWorkcell"].str.upper() == cfg["scan_stage"].upper()]
            dropped = pr_target[pr_target["SubWorkcell"].str.upper() != cfg["scan_stage"].upper()]
            print(f"    Rows that scan_stage filter KEEPS: {len(kept)}  (qty={kept['Qty'].sum()})")
            print(f"    Rows that scan_stage filter DROPS: {len(dropped)}  (qty={dropped['Qty'].sum()})")
    else:
        print("  No production CSVs loaded.")

    # ─── LAYER 2: Ingested parquet (raw_paid_hours, raw_production) ──────────
    banner(f"LAYER 2 — Ingested parquet (post-ingest, post-normalisation)")

    if MART["paid_hours"].exists():
        ph_pq = pd.read_parquet(MART["paid_hours"])
        ph_pq["date"] = pd.to_datetime(ph_pq["date"])
        ph_pq_target = ph_pq[
            (ph_pq["workcell"] == target_wc)
            & (ph_pq["date"] == target_dt)
            & (ph_pq["shift"] == target_shift)
        ]
        print(f"  raw_paid_hours.parquet rows: {len(ph_pq_target)}")
        print(f"    SUM(tph_direct) = {ph_pq_target['tph_direct'].sum():.2f}")
        if len(ph_pq_target):
            print(f"    value_type distribution: {ph_pq_target['value_type'].value_counts().to_dict()}")
    else:
        print(f"  {MART['paid_hours']} does not exist. Run ingest first.")

    if MART["production"].exists():
        pr_pq = pd.read_parquet(MART["production"])
        pr_pq["date"] = pd.to_datetime(pr_pq["date"])
        pr_pq_target = pr_pq[
            (pr_pq["workcell"] == target_wc)
            & (pr_pq["date"] == target_dt)
            & (pr_pq["shift"] == target_shift)
        ]
        print(f"  raw_production.parquet rows: {len(pr_pq_target)}")
        print(f"    SUM(qty) = {pr_pq_target['qty'].sum()}")
        if len(pr_pq_target):
            print(f"    sub_workcell distribution: {pr_pq_target['sub_workcell'].value_counts().to_dict()}")
    else:
        print(f"  {MART['production']} does not exist. Run ingest first.")

    # ─── LAYER 3: Computed parquet (ole_computed) ─────────────────────────────
    banner(f"LAYER 3 — Computed parquet (post-compute, joined with SMH)")

    if MART["ole"].exists():
        ole_pq = pd.read_parquet(MART["ole"])
        ole_pq["date"] = pd.to_datetime(ole_pq["date"])
        ole_target = ole_pq[
            (ole_pq["workcell"] == target_wc)
            & (ole_pq["date"] == target_dt)
            & (ole_pq["shift"] == target_shift)
        ]
        if len(ole_target):
            row = ole_target.iloc[0]
            print(f"  ole_computed row for this cell:")
            for col in [
                "total_qty", "effective_output_smh", "qty_missing_smh",
                "assemblies_missing_smh", "total_input_hours", "va_hours", "nva_hours",
                "ole_pct", "data_quality", "smh_coverage_pct",
            ]:
                if col in row:
                    print(f"    {col:24s} = {row[col]}")
        else:
            print(f"  No ole_computed row for {target_wc} / {target_date} / shift {target_shift}.")
    else:
        print(f"  {MART['ole']} does not exist.")

    print()
    print("-" * 70)
    print("Done. Compare to Excel/dashboard values to identify the divergent layer.")
    print("-" * 70)


if __name__ == "__main__":
    main()
