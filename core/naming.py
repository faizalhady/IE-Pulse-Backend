import pandas as pd
import json
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
NETWORK_PATH = Path(r"\\penhomev10\OLE")
RAWDATA_PATH = Path(r"\\penhomev10\OLE\RawData")

SEARCH_DIRS = [NETWORK_PATH, RAWDATA_PATH]

# ─── Configuration ────────────────────────────────────────────────────────────
# Added "WorkCell" here, though the new case-insensitive search makes it foolproof
POSSIBLE_COLUMNS = ["WorkCell", "Workcell", "SubWorkcell", "Work Center", "Line"]
FILE_PREFIXES = ["PEN_TotalProduction_", "PEN_TotalPaidHours_"]

def get_unique_workcells(directories, prefixes, target_columns):
    unique_names = set()
    files_scanned = 0
    
    # Convert target columns to lowercase once for comparison
    target_cols_lower = [col.lower() for col in target_columns]

    for directory in directories:
        if not directory.exists():
            print(f"Directory not found, skipping: {directory}")
            continue

        for prefix in prefixes:
            for file_path in directory.glob(f"{prefix}*.csv"):
                try:
                    df = pd.read_csv(file_path, dtype=str)
                    
                    # Case-insensitive column search
                    col_to_use = None
                    for actual_col in df.columns:
                        if str(actual_col).strip().lower() in target_cols_lower:
                            col_to_use = actual_col
                            break

                    if col_to_use:
                        vals = df[col_to_use].dropna().str.strip().unique()
                        unique_names.update(vals)
                        files_scanned += 1
                    else:
                        print(f"! No matching column found in: {file_path.name}. Available columns: {list(df.columns)}")

                except Exception as e:
                    print(f"X Error reading {file_path.name}: {e}")

    print(f"\nSuccessfully scanned {files_scanned} files.")
    return sorted(list(unique_names))

def main():
    print("Starting scan for unique workcells...")
    
    unique_workcells = get_unique_workcells(SEARCH_DIRS, FILE_PREFIXES, POSSIBLE_COLUMNS)

    mapping_template = {name: "" for name in unique_workcells}

    output_path = BASE_DIR / "workcell_mapping_template.json"
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(mapping_template, f, indent=4)

    print(f"OK Found {len(unique_workcells)} unique workcell variations.")
    print(f"OK Exported mapping template to: {output_path}")

if __name__ == "__main__":
    main()


# ─── canon ────────────────────────────────────────────────────────────────────
# Shared by every module (core/ is the one place allowed to be). Pure
# normalisation: uppercase alphanumerics only, so "LAM RESEARCH", "Lam-Research"
# and "LAMRESEARCH" are one key. It does NOT fold one workcell into another —
# that is what workcell_alias is for (case 2). Anything unknown keeps its own key:
# an unrecognised name must show up as itself, never silently merge into a
# neighbour. Mirrors modules/cycle_time/model_universe.norm().
import re as _re
from functools import lru_cache as _lru_cache

_NON_ALNUM = _re.compile(r"[^A-Z0-9]")


@_lru_cache(maxsize=8192)
def canon(value) -> str:
    if value is None:
        return ""
    return _NON_ALNUM.sub("", str(value).upper())
