"""completion_history - the only cycle-time mart that accumulates.

WHY
completion_status_v2 is a SNAPSHOT: every run overwrites it. That answers "where
are we now" and nothing else, so the 4Q report had no trend to plot. This module
appends one rolled-up row per ISO week so Q1 (are we improving?) and Q2 (what
changed in the last 4 weeks?) have something to read.

GRAIN
One row per (iso_week, plant, workcell, status, reason), carrying:
    models   how many models are in that bucket
    units    how much DEMAND those models represent

Units, not models, is the number that matters. Volume is heavily concentrated -
the top 500 of 4,401 demand models carry 88.1% of it, the top 100 carry 64.9% -
so a completion rate counted by model says something very different from one
weighted by what we actually build. Both are stored; the report chooses.

DEMAND ONLY, AND IT HAS TO STAY THAT WAY
The indicator is "how much of what we are BUILDING has complete cycle times".
`_completion_demand` hands out more than that: it unions the graded mart and the
whole model universe so the report can show every model that exists (57,074
rows, of which 4,401 are in demand). Rolling that up would silently redefine the
trend mid-series - the W32 snapshot on file counts 3,910 models, and the next
one would have counted 57,074 without changing a single cycle time. `rollup`
filters on `has_demand` so the caller cannot get this wrong.

ONE ROW PER WEEK, NOT PER RUN
Re-running on the same day replaces that week's rows rather than adding to them.
The completion refresh is run by hand and may go several times in a day while
someone is chasing a fix; without this the week would be counted two or three
times over and the trend would be nonsense.
"""

import logging
from datetime import datetime

import pandas as pd

from modules.cycle_time.config import CT_MART

log = logging.getLogger(__name__)

PATH = CT_MART["completion_history"]

_COLS = ["iso_week", "as_of", "plant", "workcell", "status", "reason", "models", "units"]


def iso_week(d: datetime) -> str:
    """'2026-W32'. ISO, so the week rolls on Monday like the floor's does."""
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def rollup(models: list[dict], as_of: datetime) -> pd.DataFrame:
    """The joined demand+status rows -> one row per bucket for this week."""
    if not models:
        return pd.DataFrame(columns=_COLS)
    df = pd.DataFrame(models)
    for c in ("plant", "customer", "status"):
        if c not in df.columns:
            df[c] = None
    if "reason" not in df.columns:
        df["reason"] = ""
    df["units"] = pd.to_numeric(df.get("units", 0), errors="coerce").fillna(0)
    # A missing reason and a genuinely empty one are the same thing here: the
    # status carries the whole story. Leaving NaN in would split one bucket in two.
    df["reason"] = df["reason"].fillna("")
    df["plant"] = df["plant"].fillna("Unassigned")
    # See the module docstring: the trend measures demand, and its source frame
    # carries the whole universe. Absent column = an older caller that already
    # passed demand only, so filtering on nothing is the correct no-op.
    if "has_demand" in df.columns:
        before = len(df)
        df = df[df["has_demand"].fillna(False).astype(bool)]
        if len(df) != before:
            log.info("completion history: %d of %d rows are in demand", len(df), before)
    if df.empty:
        return pd.DataFrame(columns=_COLS)

    g = (df.groupby(["plant", "customer", "status", "reason"], as_index=False)
           .agg(models=("assembly", "count"), units=("units", "sum")))
    g = g.rename(columns={"customer": "workcell"})
    g.insert(0, "as_of", as_of.isoformat(timespec="seconds"))
    g.insert(0, "iso_week", iso_week(as_of))
    g["units"] = g["units"].astype("int64")
    return g[_COLS]


def append(models: list[dict], as_of: datetime | None = None) -> pd.DataFrame:
    """Write this week's rollup, replacing any earlier rows for the same week."""
    as_of = as_of or datetime.now()
    fresh = rollup(models, as_of)
    if fresh.empty:
        log.warning("completion history: nothing to snapshot")
        return load()

    week = fresh["iso_week"].iloc[0]
    old = load()
    if not old.empty:
        old = old[old["iso_week"] != week]
    out = pd.concat([old, fresh], ignore_index=True).sort_values(["iso_week", "plant", "workcell"])

    PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(PATH, index=False)
    log.info("completion history: %s - %d buckets, %d models, %s units (%d weeks on file)",
             week, len(fresh), int(fresh["models"].sum()), f"{int(fresh['units'].sum()):,}",
             out["iso_week"].nunique())
    return out


def load() -> pd.DataFrame:
    if not PATH.exists():
        return pd.DataFrame(columns=_COLS)
    return pd.read_parquet(PATH)
