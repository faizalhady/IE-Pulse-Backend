"""
modules/ppqt/compute.py
───────────────────────
PPQT formulas, exactly as the EM-IE80-00003-B template computes them
(LAMRES 8.0 workbook, see IE-Pulse/docs/PPQT_LAMRES_DIFF.md §2-3).

Per station column (all parameters are PER STATION):

    demand_through = Σ demand            where CT > 0
    sum_dem_ct     = Σ demand × CT
    avail_sec      = days × (hours_per_day×60 − co_per_day×co_min) × 60
    WCT            = sum_dem_ct / demand_through
    Takt           = avail_sec  / demand_through
    need           = WCT / Takt                           (fractional, never rounded here)
    need_allow     = need × (1 + (1 − FPY×Eff))           "Resources NEEDED ... with Allowances"

Line group (the BOTTLENECK column): same formulas on MAX(CT of the group's
stations) per assembly, plus the CTI / PFTR block.

Report (Exe Summaries), per bay and period:

    ttl_req     = ROUNDUP(need_allow + NPI)
    variance    = available − ttl_req                     (negative = short)
    dl_required = need_allow × crew

Rounding only happens here, in the report layer.

Everything reads the marts written by pipeline/ingest.py and is cached on the
marts' mtime (core/mart_cache). `python -m modules.ppqt.compute` runs the
golden check against the workbook's own computed cells.
"""

from __future__ import annotations

import math
from functools import lru_cache

import pandas as pd

from core.mart_cache import mart_key
from modules.ppqt.config import AREA_LABELS, BAY_STATION_ALIAS, PPQT_MART


# ─── Marts ────────────────────────────────────────────────────────────────────

def _key() -> tuple:
    return mart_key(*PPQT_MART.values())


@lru_cache(maxsize=2)
def _load(_k) -> dict[str, pd.DataFrame]:
    out = {}
    for name, path in PPQT_MART.items():
        if not path.exists():
            raise FileNotFoundError(f"PPQT mart missing: {path.name} - run modules.ppqt.pipeline.refresh")
        out[name] = pd.read_parquet(path)
    return out


def marts() -> dict[str, pd.DataFrame]:
    return _load(_key())


def _sel(df: pd.DataFrame, **eq) -> pd.DataFrame:
    m = pd.Series(True, index=df.index)
    for k, v in eq.items():
        m &= df[k] == v
    return df[m]


def _records(df: pd.DataFrame) -> list[dict]:
    return df.astype(object).where(pd.notna(df), None).to_dict(orient="records")


def _ceil(x: float) -> int:
    return int(math.ceil(round(x, 9))) if x > 0 else 0


# ─── Station metrics ──────────────────────────────────────────────────────────

def _bay_lookup(m: dict, workcell: str, period: str, area: str) -> dict[str, dict]:
    """station -> Exe Summaries bay row (crew / NPI / available) for one area."""
    b = _sel(m["bays"], workcell=workcell, period=period, area_code=area)
    out = {}
    for r in b.itertuples():
        station = BAY_STATION_ALIAS.get((area, r.bay), r.bay)
        out[station] = {"bay": r.bay, "type": r.type, "crew": r.crew, "npi": r.npi,
                        "available": r.available, "dl_per_line": r.dl_per_line,
                        "sheet_line_req": r.sheet_line_req}
    return out


@lru_cache(maxsize=64)
def _station_metrics(workcell: str, area: str, period: str, _k) -> pd.DataFrame:
    m = marts()
    st = _sel(m["stations"], workcell=workcell, area=area, period=period).sort_values("seq")
    if st.empty:
        return pd.DataFrame()
    asm = _sel(m["assemblies"], workcell=workcell, area=area, period=period)[["sheet_row", "demand"]]
    ct = _sel(m["cycle_times"], workcell=workcell, area=area, period=period)[["sheet_row", "station", "ct_sec"]]
    ct = ct.merge(asm, on="sheet_row", how="inner")
    ct = ct[ct["demand"] > 0].copy()
    ct["load"] = ct["ct_sec"] * ct["demand"]

    # Per station: demand through + Σ demand×CT.
    agg = ct.groupby("station").agg(demand_through=("demand", "sum"), sum_dem_ct=("load", "sum"))
    # Per line group: MAX(CT) over the group's stations per assembly, then the same aggregates.
    grp_of = dict(zip(st["station"], st["group_no"]))
    ct["group_no"] = ct["station"].map(grp_of)
    gmax = ct.groupby(["group_no", "sheet_row"], as_index=False).agg(ct_sec=("ct_sec", "max"), demand=("demand", "first"))
    gmax["load"] = gmax["ct_sec"] * gmax["demand"]
    gagg = gmax.groupby("group_no").agg(demand_through=("demand", "sum"), sum_dem_ct=("load", "sum"))
    # CTI block needs models-with-demand per group.
    gmodels = gmax.groupby("group_no")["sheet_row"].nunique()

    bays = _bay_lookup(m, workcell, period, area)
    rows = []
    for r in st.itertuples():
        if r.is_bottleneck:
            dt = float(gagg["demand_through"].get(r.group_no, 0.0))
            sd = float(gagg["sum_dem_ct"].get(r.group_no, 0.0))
        else:
            dt = float(agg["demand_through"].get(r.station, 0.0))
            sd = float(agg["sum_dem_ct"].get(r.station, 0.0))
        daily_min = r.hours_per_day * 60 - r.co_per_day * r.co_min
        avail = r.days * daily_min * 60
        wct = sd / dt if dt > 0 else 0.0
        takt = avail / dt if dt > 0 else 0.0
        need = wct / takt if takt > 0 else 0.0
        allow = 1 + (1 - r.fpy * r.eff)
        need_allow = need * allow
        bay = bays.get(r.station)
        crew = bay["crew"] if bay else None
        npi = bay["npi"] if bay else 0.0
        have = bay["available"] if bay and bay["available"] else (r.eq_avail or None)
        ttl_req = _ceil(need_allow + npi)
        # A broken parameter cell (#REF! days) or zero available time makes the
        # sheet's need 0. Reporting "+2 spare" on that would be a lie - no verdict.
        broken = bool(r.issues) or avail <= 0
        if broken and dt > 0:
            have_for_verdict = None
        else:
            have_for_verdict = have
        row = {
            "station": r.station, "header": r.header, "seq": int(r.seq),
            "line_group": r.line_group, "group_no": int(r.group_no), "is_bottleneck": bool(r.is_bottleneck),
            "demand_through": dt, "sum_dem_ct": sd,
            "hours_per_day": r.hours_per_day, "days": r.days, "co_per_day": r.co_per_day, "co_min": r.co_min,
            "daily_avail_min": daily_min, "avail_sec": avail,
            "wct": wct, "takt": takt, "need": need, "fpy": r.fpy, "eff": r.eff, "allowance": allow,
            "need_allow": need_allow, "ttl_req": ttl_req,
            "eq_avail": r.eq_avail or None, "available": have,
            "variance": (have_for_verdict - ttl_req) if have_for_verdict is not None else None,
            "util": (need_allow / have_for_verdict) if have_for_verdict else None,
            "is_bay": bay is not None, "bay": bay["bay"] if bay else None, "type": bay["type"] if bay else None,
            "crew": crew, "npi": npi,
            "dl_required": (need_allow * crew) if crew else None,
            "sheet_need_allow": r.sheet_need_allow, "sheet_demand_through": r.sheet_demand_through,
            "delta_vs_sheet": need_allow - r.sheet_need_allow,
            "issues": r.issues or None,
        }
        if r.is_bottleneck:
            # CTI / PFTR - "use as a reference" (sheet note). Hours, not seconds.
            avail_hrs = r.hours_per_day * r.days
            avail_allow = avail_hrs * r.fpy * r.eff
            req_hrs = sd / 3600
            a = avail_allow - req_hrs
            c = a / (r.co_min / 60) if r.co_min else 0.0
            per_day = max(c / r.days, 0.0) if r.days else 0.0
            models = int(gmodels.get(r.group_no, 0))
            cti = models / per_day if per_day else 0.0
            row.update({"cti_avail_hrs": avail_hrs, "cti_avail_allow_hrs": avail_allow,
                        "cti_required_hrs": req_hrs, "cti_co_time_hrs": a,
                        "cti_possible_co": c, "cti_co_per_day": per_day,
                        "cti_models": models, "cti_days": cti, "pftr": (cti / r.days) if r.days else 0.0})
        rows.append(row)
    return pd.DataFrame(rows)


def station_metrics(workcell: str, area: str, period: str) -> pd.DataFrame:
    return _station_metrics(workcell, area, period, _key())


def area_totals(workcell: str, area: str, period: str) -> dict:
    """Sheet-level demand figures for one Area x Period (the G column total)."""
    asm = _sel(marts()["assemblies"], workcell=workcell, area=area, period=period)
    return {"total_demand": float(asm["demand"].sum()), "assemblies": int(len(asm)),
            "with_demand": int((asm["demand"] > 0).sum()),
            "demand_x_lead_hrs": float((asm["demand"] * asm["lead_time_sec"]).sum() / 3600)}


# ─── Catalogue ────────────────────────────────────────────────────────────────

def workcell_meta(workcell: str) -> dict:
    m = marts()
    st = _sel(m["stations"], workcell=workcell)
    if st.empty:
        raise KeyError(workcell)
    areas = st.groupby("area")["group_no"].first().index.tolist()
    order = [a for a in AREA_LABELS if a in areas] + [a for a in areas if a not in AREA_LABELS]
    per = _sel(m["periods"], workcell=workcell).sort_values("period")
    periods = per["period"].tolist() or sorted(st["period"].unique())
    books = _sel(m["workbooks"], workcell=workcell)
    return {
        "workcell": workcell,
        "areas": [{"code": a, "label": AREA_LABELS.get(a, a)} for a in order],
        "periods": periods,
        "latest": periods[-1] if periods else None,
        "files": _records(books),
    }


def list_workcells() -> list[dict]:
    m = marts()
    out = []
    for wc in sorted(m["stations"]["workcell"].unique()):
        meta = workcell_meta(wc)
        latest = meta["latest"]
        s = summary(wc)
        lp = next((p for p in s["periods"] if p["period"] == latest), None)
        out.append({
            "workcell": wc, "areas": [a["code"] for a in meta["areas"]], "periods": meta["periods"],
            "latest": latest,
            "volume": (lp["pca_vol"] + lp["hla_vol"]) if lp else 0,
            "dl_required": lp["dl_required"] if lp else 0,
            "actual_dl": lp["actual_dl"] if lp else 0,
            "dl_variance": lp["dl_variance"] if lp else 0,
            "bays_short": lp["bays_short"] if lp else 0,
            "equipment_short": lp["equipment_short"] if lp else 0,
            "nva_ratio": lp["nva_ratio"] if lp else None,
            "ingested_at": meta["files"][-1]["ingested_at"] if meta["files"] else None,
            "file": meta["files"][-1]["file"] if meta["files"] else None,
        })
    return out


# ─── Report (Exe Summaries) ───────────────────────────────────────────────────

@lru_cache(maxsize=16)
def _summary(workcell: str, _k) -> dict:
    m = marts()
    meta = workcell_meta(workcell)
    areas = [a["code"] for a in meta["areas"]]
    periods_df = _sel(m["periods"], workcell=workcell).sort_values("period")
    out_periods = []
    for p in periods_df.itertuples():
        metrics = {a: station_metrics(workcell, a, p.period) for a in areas}
        by_station = {a: {r["station"]: r for r in _records(df)} for a, df in metrics.items() if not df.empty}
        bays_df = _sel(m["bays"], workcell=workcell, period=p.period).sort_values("seq")
        bays, equip_short = [], 0
        for b in bays_df.itertuples():
            mrow = None
            if not b.is_overhead and b.area_code in by_station:
                mrow = by_station[b.area_code].get(BAY_STATION_ALIAS.get((b.area_code, b.bay), b.bay))
            if b.is_overhead:
                line_req, src = b.sheet_line_req, "sheet"
                dl_req = b.sheet_dl_required
            elif mrow:
                line_req, src = mrow["need_allow"], "computed"
                dl_req = line_req * b.crew
            else:
                line_req, src = b.sheet_line_req, "sheet"    # bay with no matching station column
                dl_req = line_req * b.crew
            ttl = _ceil(line_req + b.npi)
            bays.append({
                "area": b.area, "area_code": b.area_code, "is_overhead": bool(b.is_overhead),
                "bay": b.bay, "type": b.type, "dl_per_line": b.dl_per_line, "crew": b.crew,
                "line_req": line_req, "npi": b.npi, "ttl_req": ttl, "available": b.available,
                "variance": b.available - ttl, "dl_required": dl_req, "source": src,
                "station": mrow["station"] if mrow else None,
                "wct": mrow["wct"] if mrow else None, "takt": mrow["takt"] if mrow else None,
                "demand_through": mrow["demand_through"] if mrow else None,
                "sheet_line_req": b.sheet_line_req, "sheet_dl_required": b.sheet_dl_required,
            })
        # Equipment-only stations (not in the DL report) that are short.
        for a, df in metrics.items():
            if df.empty:
                continue
            eq = df[(~df["is_bay"]) & (~df["is_bottleneck"]) & df["variance"].notna() & (df["variance"] < 0)]
            equip_short += int(len(eq))

        # Demand is what moves every number - carry it at every level: the
        # period total (sheet header), each area's sheet total, each bay's
        # demand-through (on the bay rows above).
        demand_by_area = [{"area_code": a, "total_demand": area_totals(workcell, a, p.period)["total_demand"]}
                          for a in areas]
        dl_required = sum(b["dl_required"] for b in bays)
        nva_total = p.nva_dl + p.non_mfg_dl
        inline_va = dl_required - nva_total
        t = p.nva_target or 0.2
        nva_allow = inline_va * t / (1 - t) if t < 1 else 0.0
        by_area = {}
        for b in bays:
            by_area[b["area"]] = by_area.get(b["area"], 0.0) + b["dl_required"]
        out_periods.append({
            "period": p.period, "period_date": p.period_date, "weeks": p.weeks,
            "pca_vol": p.pca_vol, "hla_vol": p.hla_vol,
            "total_demand": p.pca_vol + p.hla_vol, "demand_by_area": demand_by_area,
            "dl_required": dl_required, "actual_dl": p.actual_dl, "dl_variance": p.actual_dl - dl_required,
            "nva_dl": p.nva_dl, "non_mfg_dl": p.non_mfg_dl, "nva_total": nva_total,
            "nva_ratio": (nva_total / dl_required) if dl_required else None,
            "inline_va": inline_va, "nva_target": t, "nva_allow": nva_allow, "nva_excess": nva_allow - p.nva_dl,
            "bays_short": sum(1 for b in bays if not b["is_overhead"] and b["variance"] < 0),
            "bays_total": sum(1 for b in bays if not b["is_overhead"]),
            "equipment_short": equip_short,
            "dl_by_area": [{"area": k, "dl_required": v} for k, v in by_area.items()],
            "bays": bays,
        })
    return {"workcell": workcell, "areas": meta["areas"], "periods": out_periods}


def summary(workcell: str) -> dict:
    return _summary(workcell, _key())


# ─── Assemblies ───────────────────────────────────────────────────────────────

def assemblies(workcell: str, area: str, period: str, include_all: bool = False) -> dict:
    m = marts()
    st = _sel(m["stations"], workcell=workcell, area=area, period=period).sort_values("seq")
    asm = _sel(m["assemblies"], workcell=workcell, area=area, period=period)
    if not include_all:
        asm = asm[asm["demand"] > 0]
    ct = _sel(m["cycle_times"], workcell=workcell, area=area, period=period)
    ct = ct[ct["sheet_row"].isin(asm["sheet_row"])]
    grp_of = dict(zip(st["station"], st["group_no"]))
    by_row: dict[int, dict] = {}
    for r in ct.itertuples():
        d = by_row.setdefault(r.sheet_row, {"cts": {}, "groups": {}})
        d["cts"][r.station] = r.ct_sec
        g = grp_of.get(r.station)
        if g is not None:
            d["groups"][g] = max(d["groups"].get(g, 0.0), r.ct_sec)
    rows = []
    for a in asm.sort_values("demand", ascending=False).itertuples():
        d = by_row.get(a.sheet_row, {"cts": {}, "groups": {}})
        bn = max(d["cts"].values(), default=0.0)
        rows.append({
            "assembly": a.assembly, "family": a.family, "model": a.model, "sheet_row": int(a.sheet_row),
            "demand": a.demand, "lead_time_sec": a.lead_time_sec, "bottleneck_sec": bn,
            "bottleneck_station": next((k for k, v in d["cts"].items() if v == bn), None) if bn else None,
            "demand_x_lead": a.demand * a.lead_time_sec,
            "cts": d["cts"], "group_bottleneck": {str(k): v for k, v in d["groups"].items()},
        })
    members = st[~st["is_bottleneck"]]
    return {
        "workcell": workcell, "area": area, "period": period,
        "stations": [{"station": r.station, "header": r.header, "group_no": int(r.group_no), "line_group": r.line_group}
                     for r in members.itertuples()],
        "groups": [{"group_no": int(g), "line_group": lg} for g, lg in
                   st.groupby("group_no")["line_group"].first().items()],
        "count": len(rows), "total": int(len(_sel(m["assemblies"], workcell=workcell, area=area, period=period))),
        "rows": rows,
    }


def station_assemblies(workcell: str, area: str, period: str, station: str, top: int = 25) -> dict:
    """Top contributors (demand × CT) behind one station."""
    m = marts()
    st = _sel(m["stations"], workcell=workcell, area=area, period=period)
    row = st[st["station"] == station]
    if row.empty:
        raise KeyError(station)
    asm = _sel(m["assemblies"], workcell=workcell, area=area, period=period)
    ct = _sel(m["cycle_times"], workcell=workcell, area=area, period=period)
    if bool(row.iloc[0]["is_bottleneck"]):
        members = st[(st["group_no"] == row.iloc[0]["group_no"]) & (~st["is_bottleneck"])]["station"]
        ct = ct[ct["station"].isin(members)].groupby("sheet_row", as_index=False)["ct_sec"].max()
    else:
        ct = ct[ct["station"] == station][["sheet_row", "ct_sec"]]
    df = ct.merge(asm[["sheet_row", "assembly", "model", "demand"]], on="sheet_row")
    df = df[df["demand"] > 0]
    df["load_sec"] = df["demand"] * df["ct_sec"]
    total = float(df["load_sec"].sum())
    df = df.sort_values("load_sec", ascending=False)
    df["share"] = df["load_sec"] / total if total else 0.0
    return {"station": station, "total_load_sec": total, "assemblies": int(len(df)),
            "rows": _records(df.head(top))}


# ─── 4Q ───────────────────────────────────────────────────────────────────────
# The 4Q read of PPQT, same four boxes as the OLE / Cycle Time / VA-NVA reports:
#
#   Q1  where we stand      DL coverage % per period against 100, demand behind it
#   Q2  where it is going   the shortfall ranked by bay, then the assemblies
#                           driving the top two bays
#   Q3  what we will do     the improvement plan (frontend, shared component)
#   Q4  the 100% view       where a period's capacity goes - VA, changeover,
#                           allowance, NPI, spare - summing back to 100%
#
# The one difference from the other three: they review the PAST, PPQT reviews
# the PLAN. So the trend axis is the sizing horizon, and nothing is forecast -
# demand IS the forecast.


def _capacity_split(workcell: str, period: str, areas: list[str]) -> dict:
    """Where one period's resource capacity goes, in resource-units (lines/eq).

        va + changeover + allowance + npi + spare == available

    so Q4 sums to exactly 100% the way OLE's Paynter does. Changeover is a
    capacity loss in the sheet (it shrinks avail_sec, which inflates `need`), so
    its share of `need` is split back out here to make the lever visible:

        gross      = days x hours_per_day x 3600      (before changeover)
        co_share   = 1 - avail_sec / gross
        va         = need x (1 - co_share)            what it would need with no CO
        changeover = need x co_share
        allowance  = need_allow - need                the FPY/Eff uplift
        spare      = available - need_allow - npi     negative = short

    Bay stations only (the DL report's rows); broken parameter cells are skipped
    rather than counted as zero need - see `broken` in _station_metrics.
    """
    acc = dict(va=0.0, changeover=0.0, allowance=0.0, npi=0.0, spare=0.0, available=0.0, stations=0)
    for area in areas:
        df = station_metrics(workcell, area, period)
        if df.empty:
            continue
        for r in df.itertuples():
            if not r.is_bay or r.issues or r.avail_sec <= 0 or not r.available:
                continue
            gross = r.days * r.hours_per_day * 3600
            co_share = (1 - r.avail_sec / gross) if gross > 0 else 0.0
            acc["va"] += r.need * (1 - co_share)
            acc["changeover"] += r.need * co_share
            acc["allowance"] += r.need_allow - r.need
            acc["npi"] += r.npi
            acc["available"] += r.available
            acc["stations"] += 1
    acc["spare"] = acc["available"] - (acc["va"] + acc["changeover"] + acc["allowance"] + acc["npi"])
    return acc


def fourq(workcells: tuple[str, ...], drill_top: int = 3) -> dict:
    """Everything the 4Q report needs, for one or many workcells, in one call."""
    wcs = [w for w in dict.fromkeys(workcells) if w]
    if not wcs:
        raise KeyError("no workcells picked")
    sums = {w: summary(w) for w in wcs}
    metas = {w: workcell_meta(w) for w in wcs}
    periods = sorted({p["period"] for s in sums.values() for p in s["periods"]})

    out_periods, short_acc = [], {}
    for period in periods:
        tot = dict(total_demand=0.0, dl_required=0.0, actual_dl=0.0, nva_total=0.0,
                   bays_short=0, bays_total=0, equipment_short=0)
        cap = dict(va=0.0, changeover=0.0, allowance=0.0, npi=0.0, spare=0.0, available=0.0)
        req = have = 0.0
        for w in wcs:
            sp = next((p for p in sums[w]["periods"] if p["period"] == period), None)
            if sp is None:
                continue
            for k in ("total_demand", "dl_required", "actual_dl", "nva_total",
                      "bays_short", "bays_total", "equipment_short"):
                tot[k] += sp[k]
            c = _capacity_split(w, period, [a["code"] for a in metas[w]["areas"]])
            for k in cap:
                cap[k] += c[k]
            for b in sp["bays"]:
                if b["is_overhead"]:
                    continue
                req += b["ttl_req"]
                have += b["available"]
                gap = max(0.0, -b["variance"])
                if gap <= 0:
                    continue
                k = (w, b["area_code"], b["bay"])
                a = short_acc.setdefault(k, {
                    "workcell": w, "area": b["area"], "area_code": b["area_code"], "bay": b["bay"],
                    "station": b["station"], "short": 0.0, "dl_short": 0.0, "months": 0,
                    "worst": 0.0, "worst_period": period,
                })
                a["short"] += gap
                a["dl_short"] += gap * (b["crew"] or 0)
                a["months"] += 1
                if gap > a["worst"]:
                    a.update(worst=gap, worst_period=period, station=b["station"])
        av = cap["available"] or 0.0
        out_periods.append({
            "period": period,
            **{k: tot[k] for k in tot},
            "dl_variance": tot["actual_dl"] - tot["dl_required"],
            # Q1's indicator. DL, not resources: the Exe Summaries report is a
            # headcount report, and actual_dl is a real number of people.
            "coverage_pct": (tot["actual_dl"] / tot["dl_required"] * 100) if tot["dl_required"] else None,
            "resource_req": req, "resource_avail": have,
            "capacity": cap,
            "capacity_pct": {k: (v / av * 100 if av else 0.0) for k, v in cap.items() if k != "available"},
        })

    n = len(periods) or 1
    shortfall = sorted(
        ({**a, "short_avg": a["short"] / n, "dl_short_avg": a["dl_short"] / n} for a in short_acc.values()),
        key=lambda a: a["dl_short_avg"], reverse=True)

    # Q2's two drill charts: what the top-2 short bays are actually loaded with,
    # taken from the month each bay is worst in. OLE drills to workcells; a bay
    # belongs to one workcell already, so PPQT drills to the assemblies - demand
    # x CT is what made the bay short in the first place.
    drill = []
    for a in shortfall[:2]:
        if not a["station"]:
            continue
        try:
            d = station_assemblies(a["workcell"], a["area_code"], a["worst_period"], a["station"], drill_top)
        except KeyError:
            continue
        drill.append({"workcell": a["workcell"], "bay": a["bay"], "area": a["area"],
                      "period": a["worst_period"], "station": a["station"],
                      "total_load_sec": d["total_load_sec"], "rows": d["rows"]})

    return {"workcells": wcs, "periods": out_periods, "shortfall": shortfall, "drill": drill,
            "areas": [{"code": c, "label": AREA_LABELS.get(c, c)}
                      for c in dict.fromkeys(a["code"] for m in metas.values() for a in m["areas"])]}


# ─── Inputs ───────────────────────────────────────────────────────────────────

def inputs(workcell: str) -> dict:
    m = marts()
    meta = workcell_meta(workcell)
    st = _sel(m["stations"], workcell=workcell).sort_values(["area", "period", "seq"])
    bays = _sel(m["bays"], workcell=workcell).sort_values(["period", "seq"])
    per = _sel(m["periods"], workcell=workcell).sort_values("period")
    return {
        "workcell": workcell, "areas": meta["areas"], "periods": _records(per),
        "stations": _records(st.drop(columns=["workcell"])),
        "bays": _records(bays.drop(columns=["workcell"])),
    }


# ─── Golden check ─────────────────────────────────────────────────────────────

def _selfcheck(tol: float = 1e-6) -> int:
    """Our numbers must equal the workbook's own computed cells. Returns mismatches."""
    m = marts()
    bad = 0
    keys = m["stations"][["workcell", "area", "period"]].drop_duplicates().itertuples(index=False)
    for wc, area, period in keys:
        df = station_metrics(wc, area, period)
        for r in df.itertuples():
            ref = r.sheet_need_allow
            if abs(r.need_allow - ref) > tol * max(1.0, abs(ref)):
                bad += 1
                print(f"  NEED  {wc} {area} {period} {r.station:<24} ours={r.need_allow:.6f} sheet={ref:.6f}")
            if abs(r.demand_through - r.sheet_demand_through) > tol:
                bad += 1
                print(f"  DMND  {wc} {area} {period} {r.station:<24} ours={r.demand_through} sheet={r.sheet_demand_through}")
        print(f"{wc} / {area} / {period}: {len(df)} stations checked")
    for wc in m["stations"]["workcell"].unique():
        s = summary(wc)
        for p in s["periods"]:
            for b in p["bays"]:
                if b["source"] != "computed":
                    continue
                if abs(b["line_req"] - b["sheet_line_req"]) > tol * max(1.0, abs(b["sheet_line_req"])):
                    bad += 1
                    print(f"  BAY   {wc} {p['period']} {b['bay']:<24} ours={b['line_req']:.6f} sheet={b['sheet_line_req']:.6f}")
            unmatched = [b["bay"] for b in p["bays"] if b["source"] == "sheet" and not b["is_overhead"]]
            if unmatched:
                print(f"  WARN  {wc} {p['period']} bays with no station column (sheet value used): {unmatched}")
            print(f"{wc} / {p['period']}: {len(p['bays'])} bays, DL required {p['dl_required']:.2f} "
                  f"vs actual {p['actual_dl']:.0f}, NVA ratio {p['nva_ratio']:.3f}" if p["nva_ratio"] is not None else "")
    print("PPQT golden check:", "OK" if bad == 0 else f"{bad} MISMATCHES")
    return bad


if __name__ == "__main__":
    raise SystemExit(1 if _selfcheck() else 0)
