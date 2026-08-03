"""
run_completion_v2.py — drives every phase of the completion-status v2 rebuild
and tees the whole thing to a log file.

    python scripts/run_completion_v2.py                # all phases
    python scripts/run_completion_v2.py --phase 0      # serial coverage only
    python scripts/run_completion_v2.py --no-serial    # #21 only, skip #94/#132
    python scripts/run_completion_v2.py --window 60

Phases
  0  #94 per customer -> serial index; reports how many models have a live serial
  1-2  matching ladder + new statuses          (inside completion_v2, no MES calls)
  3  #21 day-cache                             (inside completion_v2)
  4-5  classify every model, serial source preferred, batch as fallback
  6  compare v1 vs v2 and print the shift
"""
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.cycle_time import completion_v2 as v2
from modules.cycle_time.config import CT_MART

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"


def setup_log(tag: str) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    p = LOG_DIR / f"completion_v2_{datetime.now():%Y%m%d_%H%M%S}_{tag}.log"
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.FileHandler(p, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.info("log file: %s", p)
    return p


def model_list() -> pd.DataFrame:
    """Same model set v1 classified, so v1 vs v2 is a like-for-like comparison.
    customer_id comes from the MES assembly map (MES ids, not the IEDB ids in config)."""
    cs = pd.read_parquet(CT_MART["completion_status"])[["customer", "assembly"]].drop_duplicates()
    amap = pd.read_parquet(CT_MART["mes_assembly_map"])
    cid = {}
    for c, i in zip(amap["customer"], amap["customer_id"]):
        cid.setdefault(v2._cnorm(c), i)
    cs["customer_id"] = cs["customer"].map(lambda c: cid.get(v2._cnorm(c)))
    miss = cs["customer_id"].isna()
    if miss.any():
        logging.warning("no MES customer_id for: %s", ", ".join(sorted(cs.loc[miss, "customer"].unique())))
    cs = cs[~miss].copy()
    cs["customer_id"] = cs["customer_id"].astype(int)   # 59.0 -> 59; MES rejects "59.0"
    return cs.reset_index(drop=True)


# Sampling grid for #126. Each (day, hour) is one ≤30-min site-wide window.
# Spread across days AND shifts so we don't keep re-sampling the same production run.
# 28-170 days back: old enough the units have finished, recent enough to still be in MES.
# ~29 days x 6 shifts = ~174 windows (~2-4s each) to push serial coverage up from 38%.
SAMPLE_DAYS  = list(range(28, 172, 5))                # 28,33,...,168
SAMPLE_HOURS = [1, 6, 10, 14, 18, 22]                 # 6 windows across the 24h


def phase0(models: pd.DataFrame) -> pd.DataFrame:
    logging.info("=" * 78)
    logging.info("PHASE 0 — #126 serial sweep (%d windows, completed units)",
                 len(SAMPLE_DAYS) * len(SAMPLE_HOURS))
    logging.info("=" * 78)
    sidx = v2.serial_index(SAMPLE_DAYS, SAMPLE_HOURS, per_model=5)

    have = set(sidx["assembly"]) if len(sidx) else set()
    want = set(models["assembly"])
    hit = len(want & have)
    logging.info("-" * 78)
    logging.info("SERIAL COVERAGE: %d / %d models have a serial (%.0f%%)",
                 hit, len(want), 100 * hit / max(len(want), 1))
    per = (models.assign(hit=models["assembly"].isin(have))
                 .groupby("customer")["hit"].agg(["sum", "size"]))
    per["pct"] = (100 * per["sum"] / per["size"]).round(0)
    logging.info("per customer:\n%s", per.sort_values("pct", ascending=False).to_string())
    return sidx


def phase6():
    logging.info("=" * 78)
    logging.info("PHASE 6 — v1 vs v2")
    logging.info("=" * 78)
    v1 = pd.read_parquet(CT_MART["completion_status"])[["customer", "assembly", "status"]]
    n2 = pd.read_parquet(CT_MART["completion_status_v2"])
    logging.info("v1 statuses:\n%s", v1["status"].value_counts().to_string())
    logging.info("v2 statuses:\n%s", n2["status"].value_counts().to_string())
    logging.info("v2 source:\n%s", n2["source"].value_counts().to_string())
    j = v1.merge(n2[["customer", "assembly", "status"]], on=["customer", "assembly"],
                 suffixes=("_v1", "_v2"))
    logging.info("shift (v1 -> v2):\n%s",
                 j.groupby(["status_v1", "status_v2"]).size().sort_values(ascending=False).to_string())
    st = pd.read_parquet(CT_MART["completion_steps_v2"])
    logging.info("v2 MES step verdicts:\n%s",
                 st[st["side"] == "MES"]["status"].value_counts().to_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, default=None)
    ap.add_argument("--window", type=int, default=v2._WINDOW_DAYS)
    ap.add_argument("--no-serial", action="store_true")
    ap.add_argument("--no-resume", action="store_true")
    a = ap.parse_args()

    p = setup_log(f"p{a.phase}" if a.phase is not None else "all")
    models = model_list()
    logging.info("model set: %d models, %d customers", len(models), models["customer"].nunique())

    if a.phase == 0:
        phase0(models)
    elif a.phase == 6:
        phase6()
    else:
        if not a.no_serial:
            phase0(models)
        logging.info("=" * 78)
        logging.info("PHASE 1-5 — classify (window=%dd, serial=%s)", a.window, not a.no_serial)
        logging.info("=" * 78)
        from modules.cycle_time.keep_awake import keep_system_awake
        with keep_system_awake():
            v2.run(models, window=a.window, use_serial=not a.no_serial, resume=not a.no_resume)
        phase6()

    logging.info("DONE — log written to %s", p)
    print(f"\nlog: {p}")


if __name__ == "__main__":
    main()
