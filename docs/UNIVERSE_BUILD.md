# UNIVERSE — Build Document

The Jabil Universe: dimensions and facts defined once for the whole plant, so
every module becomes a query over the same tables. Phase 1 (2026-08-22) promoted
the August 2026 draft registry into tested parquet and proved the result by
computing OLE from it. Rules: the `jabil-universe` skill. Reasoning: the vault,
`Universe/Jabil Universe - Foundational Document.md`.

## Questions this build answers

```
"List all workcells — and which count is that?"
    v_workcell (status, entity_type)                              ✅ pool Q1

"Output trend of model A in workcell A, by day / ISO week / fiscal quarter"
    v_units_out_daily                                             ✅ pool Q5

"Is the universe's OLE the same as the OLE module's, and if not, why?"
    v_ole_weekly (ole_universe, ole_module, delta_pts, reason)    ✅ the proof

"Which spellings of a workcell point at two different workcells?"
    workcell_alias_conflict                                       ✅ new finding

"Where is yield lost, per step?"                                   fact_scan.test_status — view pending (Q7)
"Project next week's output from the run rate"                     fact_unit_out history — method pending (Q9)
```

## Layout

```
modules/universe/
├── config.py           paths, plant vocabulary, completion vocabulary, thresholds
├── registry.py         resolve(name) -> workcell_id — the ONE place a name becomes an id
├── views.py            the semantic layer: v_workcell · v_units_out_daily · v_ole_weekly
└── pipeline/build.py   promote registry -> tested parquet (python -m modules.universe.pipeline.build)
api/routers/universe.py GET /api/universe/health
tests/test_universe.py  30 assertions, written BEFORE each table (python tests/test_universe.py)
core/naming.py          canon() — shared normalisation (uppercase alphanumerics), no folding
data/mart/universe/     the tables
```

Sources (read-only): `C:\Users\4033375\Projects\docs\registry\` (the August draft
registry, 107 files) · `C:\Users\4033375\Projects\temp\workcell group.xlsx` (Faiz's
plant/region sheet, 2026-08-06) · `data/mart/ole/ole_weekly.parquet` (compared
against, never computed from).

## Tables — grain first

| Table | One row per | Rows | Notes |
|---|---|---|---|
| `dim_workcell` | workcell (= customer) | 111 | 42 active + the UNKNOWN member (id 0). `plant_physical` ≠ `plant_governing` for MICRON SIG, LAMGB, LAMMEC. `parent_id` NULL — families unverified; August's guess kept as `parent_id_proposed` |
| `workcell_alias` | (workcell, system, value) | 537 | 13 source systems + `workcell_group_sheet`. Canonical row wins on conflict |
| `workcell_alias_conflict` | spelling that points at 2+ workcells | 7 | e.g. `TELLABS` → 44 (its own row) and 101 (INFINERA NEW, where its cycle-time data folds). Surfaced, never resolved |
| `dim_calendar` | date, 2019-09-01 → 2031-12-31 | 4,505 | fiscal year starts September; ISO week and Jabil work week separate |
| `dim_shift` | shift code | 3 | production on 2 and 3 only (07:00 / 19:00) |
| `dim_model` | (workcell, assembly) | 167,384 | 2,111 models have no known workcell — kept, `workcell_id` NULL |
| `dim_model_revision` | (model, revision) | 225,872 | BOM and route hang off the revision |
| `fact_scan` | board × step (MES `WipScanData`) | 18,747,552 | 9 Jul → 8 Aug 2026. 1,094,216 duplicate keys from overlapping hourly pulls removed. `shift` and `shift_date` from the LOCAL clock |
| `model_terminal_step` | model | 7,286 | the step its boards finish at, learned from history; 4,682 learned (93% of models with ≥ 5 boards); `terminal_kind` packout / link / other |
| `fact_unit_out` | board that completed | 1,670,993 | one row per board, at its model's terminal step; scrapped boards excluded |
| `fact_paid_hours` | (employee, date, shift, workcell, sub-workcell) | 324,546 | 2,563 same-key rows summed (rolling-window overlaps, case 43) |
| `dim_smh` | (workcell, model, scan_stage) | 30,728 | standard man-hours per unit, latest update wins |
| `ole_reconciliation` | (workcell, ISO week) | 162 | 19 weeks comparable with the OLE module; every delta > 2 pts carries a computed reason |

## The proof — OLE from the universe vs the OLE module (W28–W31 2026)

`OLE = Σ(units_out × SMH) ÷ Σ paid_hours × 100`, universe tables only.

| Workcell | Weeks | Result |
|---|---|---|
| **COLLINS** | W29–W31 | **within 2 points on every full week** (−1.0, −1.7, +1.4). When the inputs agree, the universe computes OLE correctly |
| ASP (FORTIVE) | W29–W31 | +7.0, +6.4, −1.8 — 35–87% of units carry no SMH; the module *estimates* a standard for those, the universe earns zero |
| BECKMAN COULTER | W29–W31 | +16 to +26 — the universe counts 13–18% more units (boards once at the terminal step vs the module's share quantities at its scan stage). Both sides > 100% (case 19) |
| WABTEC | W29–W31 | −13 to −34 — 19–52% of units without SMH, plus the unit definition |
| LAM RESEARCH | W28–W31 | −72 to −88 — **paid hours are 5× the module's** (52k vs 10k). The registry folds the LAM family's payroll (LAMMEC, LAMGB, …) into LAM RESEARCH; the module counts one cost centre. The roots/subs question, in numbers |

Every delta has a reason in the table. Nothing was tuned to agree.

## What Phase 1 found (→ gotchas register, cases 59–63)

1. **The alias table carries two meanings.** `cycle_time` aliases encode *folding*
   (this customer's cycle-time data is counted under that workcell); `mes_name` points
   at the customer's own row. 7 spellings → 2 ids. Canonical row wins; conflicts recorded.
2. **Overlapping hourly pulls duplicate scans** — 1,094,216 of 19,841,768 raw rows.
   Dedupe on (wip_id, step, step_instance, completed_at_utc).
3. **LINK is after completion.** 507k boards' last scan is LINK (the logistics scan
   ~5.7 h after PACKOUT). 66 KEYSIGHT models end at LINK with no PACKOUT in the window —
   child boards consumed into a parent. Counted, flagged `terminal_kind = link`, open.
4. **The OLE module estimates SMH for units that have none; the universe does not.**
   A definition choice that moves OLE by 5–35 points where coverage is poor.
5. **Payroll scope follows the family.** LAM RESEARCH's paid hours in the registry are
   the whole LAM family's. Until roots/subs are a fact, the universe's OLE for
   multi-sub families is not comparable with the module's.

## Open — decided later, on purpose

- Bay identity (case 9) — `fact_scan.bay_id` carries MES's scheme; no `dim_bay` yet.
- Which of shift 2 / 3 is "morning" (`shift_name_raw` holds the August guess).
- Terminal step for thin-history models — 2,339 models default to PACKOUT.
- MES refresh pipeline — the pull stops 8 Aug 2026 (Phase 2).
- `dim_process`, `dim_bay`, `dim_employee`, `dim_asset` — waves 3–5.

## Commands

```
python -m modules.universe.pipeline.build     rebuild every table from the sources (~3 min)
python tests/test_universe.py                 the 30 assertions
python -m modules.universe.views              print every view's columns and comments
python -m modules.universe.registry           resolve a few names
```

Branch: `universe/phase-1`. Not merged. Nothing on 02.
