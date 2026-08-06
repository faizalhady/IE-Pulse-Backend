"""
line_metrics.py  (cycle_time)
─────────────────────────────
Per-model LBR (Line Balance Rate) and IPK (In-Process Kanban / trolley) from the
IEDB route. Math ported from the `preflight` project's engines (src/engines/
lbr.ts + ipk.ts) — model-agnostic pure functions fed one model's priority-1 steps.

Only meaningful when the model's cycle-time data is COMPLETE (see completion_status).

Per step:  effUPH = (3600 / CT) × fpy × eff        (CT = cycle_time_per_process)
           operator step ⇔ hand > 0 OR imt > 0
LBR:  bottleneck = max(CT over ALL steps);  n0 = #operator steps;
      TCTo = Σ CT over operator steps;  LBR% = 100 × TCTo / (bottleneck × n0)
      balance_line = TCTo / n0            (the perfectly-levelled operator height)
IPK:  group consecutive same-`grp` steps → groupingUPH = min(effUPH);
      between adjacent groups: gap = UPH_up − UPH_down; hours = LOADING / UPH_up;
      ipkUnits = max(0, gap × hours);  trolleys = ceil(ipkUnits / boards_per_trolley)

Writes line_metrics.parquet (customer, assembly, lbr, ipk_trolleys, n0,
bottleneck_ct, station_count). Full station/buffer breakdown is computed
on-demand by compute_one() for the drawer. LOCAL — reads raw.parquet, no MES.
"""

import logging
import math

import duckdb
import pandas as pd

from modules.cycle_time.config import CT_MART

log = logging.getLogger(__name__)

LOADING = 500            # lot/run window units the buffer is sized to (preflight lotSize)
BOARDS_PER_TROLLEY = 20  # flat default (per-model trolley metrics come later)
DEFAULT_FPY = 1.0
DEFAULT_EFF = 0.85


def _num(v) -> float:
    """None / NaN → 0.0 (raw stores blanks as NaN, not 0)."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return 0.0
    return float(v)


def _frac(v, default):
    """Normalise fpy/eff to a 0–1 fraction (values >1 are percents)."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return default
    v = float(v)
    return v / 100.0 if v > 1 else v


def _process_label(step: str) -> str:
    """Display label = everything before the first ' - ' (preflight processName)."""
    return str(step).split(" - ")[0].strip()


# Machine/test-paced steps: run unattended (often parallel/offline), so they set
# their own pace, not the manual-line balance. IEDB sometimes records their long
# duration under `hand`, so a name filter backs up the mach/imt/hand split.
# v1 default — the IE team can refine this list.
import re as _re
_MACHINE_KW = _re.compile(
    r"\b(TEST|FVT|BURN[\s-]?IN|OVEN|REFLOW|WAVE|WASH|BAKE|CURE|COAT|AOI|X[\s-]?RAY|"
    r"ICT|SPI|HI[\s-]?POT|ESS|DISPENSE|SMT|ASOLDER|SOLDER PASTE|ROUTER|DEPANEL|LASER|PRESS)\b",
    _re.I)


def compute_metrics(steps: list[dict], loading: int = LOADING,
                    boards_per_trolley: int = BOARDS_PER_TROLLEY) -> dict | None:
    """steps: priority-1 rows for ONE model, each with keys
    step, ct, step_order, grp, sub_workcenter, hand, imt, mach, fpy, eff.
    LBR + IPK are computed over the OPERATOR (manual) stations only — station load =
    imt + hand; machine/test-paced steps are excluded (they don't set operator balance).
    Returns the summary + breakdown, or None if no operator stations."""
    rows = []        # operator-only → LBR balance
    flow_rows = []   # EVERY step (incl. machine/test) → IPK material flow
    for s in steps:
        ct = s.get("ct")
        if ct is None or (isinstance(ct, float) and math.isnan(ct)) or ct <= 0:
            continue
        hand, imt, mach = _num(s.get("hand")), _num(s.get("imt")), _num(s.get("mach"))
        op_ct = imt + hand                        # human-attended time = the balance load
        label = _process_label(s.get("step"))
        fpy, eff = _frac(s.get("fpy"), DEFAULT_FPY), _frac(s.get("eff"), DEFAULT_EFF)
        order = s.get("step_order") if s.get("step_order") is not None else 1e9
        sw = s.get("sub_workcenter")
        # IPK flow: throughput UPH from the FULL station cycle time (machine/test included —
        # an oven or burn-in is a real material-flow buffer point).
        flow_rows.append({
            "step": label, "ct": ct, "order": order, "grp": s.get("grp"),
            "sub_workcenter": sw, "uph": (3600.0 / ct) * fpy * eff,
        })
        is_machine = op_ct <= 0 or mach > op_ct or bool(_MACHINE_KW.search(label))
        if is_machine:
            continue                              # excluded from operator balance (LBR only)
        rows.append({
            "step": label, "ct": op_ct, "order": order, "grp": s.get("grp"),
            "sub_workcenter": sw, "is_operator": True,
            "uph": (3600.0 / op_ct) * fpy * eff,
        })
    if not flow_rows:
        return None
    rows.sort(key=lambda r: (r["order"], r["step"]))
    flow_rows.sort(key=lambda r: (r["order"], r["step"]))

    # ── LBR — computed PER LINE (sub_workcenter); operators are balanced within a
    # line, not across the whole route (an SMT op isn't balanced vs a packout op) ──
    lines_map: dict = {}
    for r in rows:
        lines_map.setdefault(r["sub_workcenter"] or "—", []).append(r)

    lines = []
    for sw, lrows in lines_map.items():
        bn = max(x["ct"] for x in lrows)
        bn_step = max(lrows, key=lambda x: x["ct"])["step"]
        n, t = len(lrows), sum(x["ct"] for x in lrows)
        lines.append((lrows[0]["order"], {   # lrows is order-sorted → [0] = earliest step
            "sub_workcenter": sw,
            "lbr": round(100.0 * t / (bn * n), 1) if (bn > 0 and n > 0) else None,
            "n0": n, "bottleneck_ct": round(bn, 1), "bottleneck_step": bn_step,
            "balance_line": round(t / n, 1) if n > 0 else None,
            "stations": [{"step": x["step"], "ct": round(x["ct"], 1),
                          "is_bottleneck": (x["step"] == bn_step and x["ct"] == bn)} for x in lrows],
        }))
    lines.sort(key=lambda t: t[0])                 # process order — line that runs first shows first
    lines = [l for _, l in lines]

    # headline LBR = station-weighted average across the model's lines
    tot_n = sum(l["n0"] for l in lines)
    valid = [l for l in lines if l["lbr"] is not None]
    lbr = round(sum(l["lbr"] * l["n0"] for l in valid) / sum(l["n0"] for l in valid), 1) if valid else None
    n0 = tot_n
    bottleneck = max((r["ct"] for r in rows), default=0.0)
    bn_step = max(rows, key=lambda r: r["ct"])["step"] if rows else None

    # ── IPK: per LINE (sub_workcenter), the FULL flow — every process shown with its
    #    UPH; a trolley buffer sits at each step→next gap where upstream outruns down. ──
    flow_map: dict = {}
    for r in flow_rows:
        flow_map.setdefault(r["sub_workcenter"] or "—", []).append(r)

    ipk_lines = []
    for sw, frows in flow_map.items():             # frows already order-sorted (stable)
        stations = [{"step": r["step"], "uph": round(r["uph"], 1), "ct": round(r["ct"], 1)}
                    for r in frows]
        bufs = []
        for i in range(len(frows) - 1):
            up, down = frows[i], frows[i + 1]
            gap = up["uph"] - down["uph"]
            hours = loading / up["uph"] if up["uph"] > 0 else 0
            ipk_units = max(0.0, gap * hours)
            trolleys = math.ceil(ipk_units / boards_per_trolley) if ipk_units > 0 else 0
            bufs.append({
                "from": up["step"], "to": down["step"],
                "up_uph": round(up["uph"], 1), "down_uph": round(down["uph"], 1),
                "gap": round(gap, 1), "ipk_units": round(ipk_units), "trolleys": trolleys,
            })
        ipk_lines.append({"sub_workcenter": sw, "_ord": frows[0]["order"],
                          "stations": stations, "buffers": bufs,
                          "trolleys": sum(b["trolleys"] for b in bufs)})
    ipk_lines.sort(key=lambda l: l["_ord"])         # process order — first line first
    for l in ipk_lines:
        l.pop("_ord")
    buffers = [b for l in ipk_lines for b in l["buffers"]]   # flat, for total/back-compat
    ipk_trolleys = sum(b["trolleys"] for b in buffers)

    return {
        "lbr": lbr, "n0": n0,
        "bottleneck_ct": round(bottleneck, 1), "bottleneck_step": bn_step,
        "station_count": len(rows),
        "ipk_trolleys": ipk_trolleys, "loading": loading,
        "boards_per_trolley": boards_per_trolley,
        "lines": lines, "ipk_lines": ipk_lines, "buffers": buffers,
    }


def _steps_query(where: str) -> str:
    raw = CT_MART["raw"].as_posix()
    eff = CT_MART["eff_by_line"]
    eff_join = ""
    eff_sel = "CAST(NULL AS DOUBLE) AS eff"
    if eff.exists():
        eff_sel = "el.eff AS eff"
        eff_join = f"LEFT JOIN read_parquet('{eff.as_posix()}') el " \
                   f"ON el.customer = r.customer AND el.sub_workcenter = r.sub_workcenter"
    return f"""
        SELECT r.customer, r.assembly, r.process AS step, r.cycle_time_per_process AS ct,
               r."order" AS step_order, r.grp, r.sub_workcenter,
               r.hand, r.imt, r.mach, r.fpy, {eff_sel}
        FROM read_parquet('{raw}') r {eff_join}
        WHERE {where} AND r.priority = 1 AND r.cycle_time_per_process IS NOT NULL
    """


def compute_one(customer: str, assembly: str) -> dict | None:
    """Full LBR + IPK breakdown for one model (drawer). Customer match is
    case-insensitive — the FE passes the buildplan name (e.g. 'RESMED') while raw
    stores the config name ('ResMed')."""
    con = duckdb.connect()
    df = con.execute(_steps_query("lower(r.customer) = lower(?) AND r.assembly = ?"), [customer, assembly]).fetchdf()
    con.close()
    if df.empty:
        return None
    return compute_metrics(df.to_dict("records"))


def build() -> int:
    """Compute LBR + IPK summary for every model with priority-1 CT data → mart.
    LOCAL (raw + eff_by_line), no MES. Returns row count."""
    con = duckdb.connect()
    df = con.execute(_steps_query("TRUE")).fetchdf()
    con.close()
    log.info("line_metrics: %d priority-1 CT rows across %d models",
             len(df), df[["customer", "assembly"]].drop_duplicates().shape[0])

    out = []
    for (cust, asm), g in df.groupby(["customer", "assembly"]):
        m = compute_metrics(g.to_dict("records"))
        if not m:
            continue
        out.append({"customer": cust, "assembly": asm, "lbr": m["lbr"],
                    "ipk_trolleys": m["ipk_trolleys"], "n0": m["n0"],
                    "bottleneck_ct": m["bottleneck_ct"], "station_count": m["station_count"]})

    res = pd.DataFrame(out, columns=["customer", "assembly", "lbr", "ipk_trolleys",
                                     "n0", "bottleneck_ct", "station_count"])
    CT_MART["line_metrics"].parent.mkdir(parents=True, exist_ok=True)
    res.to_parquet(CT_MART["line_metrics"], index=False)
    log.info("line_metrics.parquet written (%d models) -> %s", len(res), CT_MART["line_metrics"])
    return len(res)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    build()
