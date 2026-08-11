"""
modules/ole/smh_scope.py
────────────────────────
Scopes the SMH coverage list to models worth chasing, and adds the ones that
are coming but have never been built.

Why this exists: the raw list is "everything ever scanned in MES", a window that
only grows. A model built once in March counts against coverage forever, while a
model the planner has scheduled for next week is invisible until the day it is
first built — by which point its units have already earned nothing.

Three tiers, anchored to the newest production date in the mart (NOT today's
calendar date — if the pipeline stalls for a week, the boundary must not slide
and silently reclassify models):

  ACTIVE    built in the last 90 days, or on the planner's 13-week horizon.
            This is the list that matters and the one coverage % should mean.
  UPCOMING  on the planner horizon, with no build in the MES window. NOT the
            same as "never built": that window only reaches back to the oldest
            file the share still had, so a model last built before it looks
            identical to a brand-new one. Either way there is no measurement
            behind it, which is the point.
  DORMANT   built once, long ago, nothing planned. Kept visible, out of the
            headline, so nobody spends a morning chasing a model from March.

The line problem
────────────────
SMH is keyed by (workcell, assembly), and an OLE workcell is a LINE at a STAGE,
not a customer. AOP1 is a shared SMT line many customers run on; LAM RESEARCH is
a customer's own Backend line. 682 models are built at both — SMT on the flex
line, Backend on the dedicated one. Those are two different pieces of work, each
earning its own hours, each needing its own SMH. Not duplicates.

A never-built model therefore has no line, and one has to be chosen before an SMH
row can exist. We file it under EVERY line that customer's models historically
land on, learned from production rather than hardcoded (see `customer_lines`).
Some of those rows will be for a line that never builds it; that is the accepted
cost of being ready on day one.
"""

import logging
import re

import pandas as pd

log = logging.getLogger(__name__)

ACTIVE_DAYS = 90        # MES lookback
PLANNER_WEEKS = 13      # forward horizon; matches modules/cycle_time PLANNER_WEEKS

# A line has to account for at least this share of a customer's matched models
# before an unbuilt model is filed under it. Without a floor, one stray model
# built once on the wrong line would add that line to every future model.
MIN_LINE_SHARE = 0.05


def norm(s) -> str:
    """Assembly keys, punctuation-insensitive.

    Planner spreadsheets and MES disagree on dashes and spacing for the same
    part. This is the house join key — never join these sources on workcell,
    whose names differ per source in ways no rule can reconcile.
    """
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def customer_lines(built: pd.DataFrame, plan: pd.DataFrame) -> dict[str, list[str]]:
    """Which OLE lines each planner customer's models actually land on.

    Learned from history, not configured: planner says `LAMRESEARCH`, OLE says
    `LAM RESEARCH` and `AOP1`, and no string rule gets you from one to the other.
    Matching on the assembly key and reading off the answer does.
    """
    pairs = (plan[["n", "customer"]].drop_duplicates()
             .merge(built[["n", "workcell"]].drop_duplicates(), on="n"))
    out = {}
    for cust, g in pairs.groupby("customer"):
        share = g.workcell.value_counts(normalize=True)
        lines = share[share >= MIN_LINE_SHARE].index.tolist()
        if lines:
            out[cust] = lines
    return out


def apply_scope(status: pd.DataFrame, planner: pd.DataFrame | None) -> pd.DataFrame:
    """Add `tier` / `next_build_*` to the coverage rows, and append UPCOMING ones.

    `status` is compute.py's smh_assembly_status (one row per built line+model).
    `planner` may be None — the demand mart is a separate pipeline, and OLE must
    still compute without it. In that case every row is scored on MES alone and
    nothing is marked UPCOMING.
    """
    status = status.copy()
    status["n"] = status["assembly"].map(norm)
    status["last"] = pd.to_datetime(status["last_seen_date"], errors="coerce")

    anchor = status["last"].max()
    recent_cut = anchor - pd.Timedelta(days=ACTIVE_DAYS)

    status["next_build_date"] = pd.NaT
    status["next_build_qty"] = 0

    if planner is None or planner.empty:
        status["tier"] = pd.Series(["ACTIVE"] * len(status)).where(
            status["last"].ge(recent_cut).values, "DORMANT")
        log.warning("No planner demand -- UPCOMING skipped, tiers from MES only.")
        return status.drop(columns=["n", "last"])

    plan = planner.copy()
    plan["n"] = plan["model"].map(norm)
    plan["customer"] = plan["workcell"]
    plan["period_start"] = pd.to_datetime(plan["period_start"], errors="coerce")

    # Forward horizon only, measured from the same anchor as the lookback so the
    # two windows can never drift apart.
    horizon = plan[(plan["period_start"] >= anchor)
                   & (plan["period_start"] <= anchor + pd.Timedelta(weeks=PLANNER_WEEKS))
                   & (plan["qty"] > 0)]

    nxt = (horizon.sort_values("period_start").groupby("n")
           .agg(next_build_date=("period_start", "first"),
                next_build_qty=("qty", "first")))
    planned = set(nxt.index)

    status["next_build_date"] = status["n"].map(nxt["next_build_date"])
    status["next_build_qty"] = status["n"].map(nxt["next_build_qty"]).fillna(0).astype(int)

    status["tier"] = "DORMANT"
    status.loc[status["last"] >= recent_cut, "tier"] = "ACTIVE"
    status.loc[status["n"].isin(planned), "tier"] = "ACTIVE"

    # ── UPCOMING: planned, with no build anywhere in the MES window ───────────
    lines = customer_lines(status, horizon)
    built_keys = set(status["n"])
    rows = []
    for n, g in horizon[~horizon["n"].isin(built_keys)].groupby("n"):
        first = g.sort_values("period_start").iloc[0]
        for line in lines.get(first["customer"], []):
            rows.append({
                "workcell": line,
                "assembly": first["model"],
                "smh_value": 0.0,
                "total_qty_produced": 0,
                "first_seen_date": pd.NaT,
                "last_seen_date": pd.NaT,
                "active_days": 0,
                "smh_status": "NOT_IN_SMH_DB",
                "tier": "UPCOMING",
                "next_build_date": first["period_start"],
                "next_build_qty": int(first["qty"]),
            })

    if rows:
        status = pd.concat([status.drop(columns=["n", "last"]),
                            pd.DataFrame(rows)], ignore_index=True)
    else:
        status = status.drop(columns=["n", "last"])

    log.info("SMH scope (anchor %s): %s", anchor.date(),
             status["tier"].value_counts().to_dict())
    return status
