"""
tools.py  (cycle_time.chat)
───────────────────────────
The tools the model may call, and the code behind them.

EVERY TOOL IS AN EXISTING, TESTED PATH
  Nothing here computes a completion percentage. Each tool reads the same
  functions the frontend reads — `_completion_demand`, `ct_completion_history`,
  `bom.for_model` — so the chatbot cannot disagree with the screen. The moment a
  tool re-derives a number, we are back to one workcell reporting 279, 236 and
  208 complete models on three screens.

TERSE DESCRIPTIONS, MEASURED
  One line each. llama3.1:8b routed 6/7 realistic questions on descriptions this
  short. Rewriting them "properly" — a sentence of context per argument — dropped
  it to inventing arguments that do not exist
  (`{"workcell_completion": "PERCENTAGE", "status_breakdown": "false"}`) and, on
  one question, calling nothing at all. Small models read a schema as a pattern to
  imitate, so a long schema invites long output.

THE MODEL WILL SEND ARGUMENTS THAT DO NOT EXIST
  So `call()` drops anything not in the signature rather than raising. A crash on
  a hallucinated key would fail a question the model otherwise routed correctly.

EVERY RESULT CARRIES ITS SOURCE
  `_src` names what produced the number. It is rendered under the answer, because
  this is meant to replace IEDB, and nobody switches to a system they cannot check.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import pandas as pd

from modules.cycle_time.chat import resolve

log = logging.getLogger(__name__)

# ─── shared reads ────────────────────────────────────────────────────────────


def _demand(scope: str = "demand") -> pd.DataFrame:
    """The joined demand+status frame every screen reads. `scope="all"` widens to
    every model that exists."""
    from api.routers.cycle_time import _completion_demand, _completion_demand_key
    d = pd.DataFrame(_completion_demand(_completion_demand_key())["models"])
    if scope != "all" and "has_demand" in d:
        d = d[d["has_demand"]]
    return d


def _pct(part: float, whole: float) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _rollup(d: pd.DataFrame) -> dict:
    """Completion both ways, plus the status split. Units is the headline; models
    is the work list. Returning only one is how the two get confused."""
    u = float(pd.to_numeric(d.get("units"), errors="coerce").fillna(0).sum())
    cu = float(pd.to_numeric(d.loc[d["status"] == "complete", "units"], errors="coerce").fillna(0).sum())
    by = d["status"].value_counts().to_dict()
    return {
        "models": int(len(d)), "complete_models": int(by.get("complete", 0)),
        "pct_by_models": _pct(by.get("complete", 0), len(d)),
        "units": int(u), "complete_units": int(cu), "pct_by_units": _pct(cu, u),
        "by_status": {k: int(v) for k, v in by.items()},
    }


# ─── the tools ───────────────────────────────────────────────────────────────


def list_workcells() -> dict:
    """Just the names.

    It used to return a model count per workcell, and that was actively harmful:
    asked "how many models are in keysight" the model routed here, got a list of
    every workcell, and answered "34" for a workcell that has 744. A tool whose
    result contains many plausible numbers invites a wrong one to be picked. Counts
    come from workcell_completion, which answers about exactly one workcell.
    """
    d = _demand()
    names = sorted({str(w) for w in d["customer"].dropna() if str(w).strip("- ")})
    return {"_src": "completion demand mart", "count": len(names), "workcells": names}


def workcell_completion(workcell: str, scope: str = "demand") -> dict:
    """Completion % and status breakdown for one workcell."""
    wc = resolve.workcell(workcell)
    d = _demand(scope)
    d = d[d["customer"].astype(str) == wc]
    if d.empty:
        return {"_src": "completion demand mart", "workcell": wc, "models": 0,
                "note": f"{wc} has no models in this scope ({scope})."}
    return {"_src": f"completion demand mart, scope={scope}", "workcell": wc, "scope": scope, **_rollup(d)}


def plant_completion(scope: str = "demand") -> dict:
    """Overall completion across every workcell, plus the per-workcell ranking."""
    d = _demand(scope)
    per = []
    for wc, g in d.groupby("customer"):
        if not str(wc).strip("- "):
            continue
        r = _rollup(g)
        per.append({"workcell": str(wc), "models": r["models"],
                    "pct_by_units": r["pct_by_units"], "units": r["units"]})
    per.sort(key=lambda r: -(r["units"] or 0))
    return {"_src": f"completion demand mart, scope={scope}", "scope": scope,
            "overall": _rollup(d), "by_workcell": per[:40]}


def model_status(workcell: str, assembly: str) -> dict:
    """Completion verdict for one model."""
    wc, asm = resolve.assembly(assembly, workcell or None)
    d = _demand("all")
    n = lambda s: s.astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
    hit = d[(n(d["customer"]) == n(pd.Series([wc]))[0]) & (n(d["assembly"]) == n(pd.Series([asm]))[0])]
    if hit.empty:
        return {"_src": "completion demand mart", "workcell": wc, "assembly": asm,
                "note": "model exists but has no completion row yet."}
    r = hit.iloc[0]
    keep = ("status", "reason", "units", "has_demand", "coverage", "expected",
            "present", "no_ct", "not_in_iedb", "unmapped", "graded_on")
    return {"_src": "completion demand mart", "workcell": wc, "assembly": asm,
            **{k: (None if pd.isna(r.get(k)) else r.get(k)) for k in keep if k in hit.columns}}


def model_bom(workcell: str, assembly: str) -> dict:
    """MES BOM materials for one model."""
    from modules.cycle_time.bom import for_model
    wc, asm = resolve.assembly(assembly, workcell or None)
    b = for_model(wc, asm)
    mats = b.pop("materials", [])
    return {"_src": "MES Bom/GetBOMMaterialsByBOM via bom_material mart", **b,
            "material_count": len(mats), "materials": mats[:40]}


def model_cycle_time(workcell: str, assembly: str) -> dict:
    """The IEDB cycle-time steps for one model."""
    from api.routers.cycle_time import ct_assembly_builds
    wc, asm = resolve.assembly(assembly, workcell or None)
    rows = ct_assembly_builds(customer=wc, assembly=asm, sub_workcenter=None)
    steps = [{k: r.get(k) for k in ("revision", "sub_workcenter", "step", "seconds")}
             for r in (rows or [])][:60]
    total = sum(float(s["seconds"] or 0) for s in steps)
    return {"_src": "IEDB raw.parquet via /assembly-builds", "workcell": wc, "assembly": asm,
            "step_count": len(steps), "total_seconds": round(total, 1), "steps": steps}


def models_by_status(workcell: str, status: str, limit: int = 25) -> dict:
    """List the models in one workcell that carry a given status."""
    wc = resolve.workcell(workcell)
    st = resolve.status(status)
    d = _demand()
    d = d[(d["customer"].astype(str) == wc) & (d["status"] == st)]
    d = d.sort_values("units", ascending=False)
    return {"_src": "completion demand mart, scope=demand", "workcell": wc, "status": st,
            "total": int(len(d)),
            "models": [{"assembly": str(r.assembly), "units": int(r.units or 0),
                        "reason": (None if pd.isna(r.reason) else r.reason)}
                       for r in d.head(int(limit)).itertuples()]}


def search_models(query: str, limit: int = 20) -> dict:
    """Find models whose part number contains the text."""
    hits = resolve.search(query, int(limit))
    return {"_src": "model_universe mart", "query": query, "count": len(hits), "models": hits}


#: Values that mean "no filter". Half are the user's words, half are what a small
#: model writes into an optional argument it has nothing to put in.
_MEANS_EVERYTHING = {"all", "plant", "everything", "overall", "total",
                     "none", "null", "n/a", "na", "undefined", "-"}


def completion_trend(workcell: str = "") -> dict:
    """Week-by-week completion and the ranked losses."""
    from api.routers.cycle_time import ct_completion_history
    wc = None
    # The model fills an optional string argument with whatever looks like empty
    # to it — "None", "null", "N/A" — and a literal "None" then resolves to
    # nothing and the whole question dies on a name the user never typed.
    if workcell and workcell.strip().lower() not in _MEANS_EVERYTHING:
        wc = resolve.workcell(workcell)
    h = ct_completion_history(plants=None, workcells=wc, weeks=13)
    return {"_src": "completion_history mart (weekly snapshots), demand units",
            "workcell": wc or "all", "weeks": h.get("weeks", []),
            "latest": h.get("latest"), "losses": (h.get("losses") or [])[:8]}


# ─── registry ────────────────────────────────────────────────────────────────

#: (function, one-line description, {arg: json type}). Descriptions stay ONE line
#: — see the module docstring for what happened when they did not.
_REGISTRY: list[tuple[Callable, str, dict]] = [
    (list_workcells,      "Names of the workcells. Only when the user named none.", {}),
    (workcell_completion, "How many models, and completion %, for ONE named workcell.", {"workcell": "string"}),
    (plant_completion,    "Overall completion across all workcells, ranked.", {}),
    (model_status,        "Completion verdict for ONE model.", {"workcell": "string", "assembly": "string"}),
    (model_bom,           "MES BOM materials for one model.", {"workcell": "string", "assembly": "string"}),
    (model_cycle_time,    "IEDB cycle-time steps for one model.", {"workcell": "string", "assembly": "string"}),
    (models_by_status,    "Models in a workcell with a status: complete, incomplete, no_cycle_time, not_in_iedb, not_built, cannot_check.",
                          {"workcell": "string", "status": "string"}),
    (search_models,       "Find models whose part number contains some text.", {"query": "string"}),
    (completion_trend,    "Weekly completion trend and top losses.", {"workcell": "string"}),
]

FUNCS: dict[str, Callable] = {f.__name__: f for f, _, _ in _REGISTRY}


def schema() -> list[dict]:
    """Ollama/OpenAI tool schema."""
    out = []
    for fn, desc, args in _REGISTRY:
        out.append({"type": "function", "function": {
            "name": fn.__name__, "description": desc,
            "parameters": {"type": "object",
                           "properties": {k: {"type": v} for k, v in args.items()},
                           "required": list(args)}}})
    return out


def call(name: str, args: dict | None) -> dict:
    """Run one tool. Unknown name, bad argument or unresolvable value all come
    back as a RESULT the model can act on, never an exception — a question that
    routed correctly should not 500 because a name was ambiguous."""
    fn = FUNCS.get(name)
    if fn is None:
        return {"error": f"no such tool {name!r}", "available": sorted(FUNCS)}

    # The model invents arguments. Keep only the ones the function declares.
    import inspect
    allowed = set(inspect.signature(fn).parameters)
    clean = {k: v for k, v in (args or {}).items() if k in allowed}
    dropped = sorted(set((args or {}) ) - allowed)
    if dropped:
        log.info("chat: dropped invented args for %s: %s", name, dropped)

    try:
        return fn(**clean)
    except resolve.Ambiguous as e:
        return {"error": "ambiguous", "kind": e.kind, "given": e.given, "options": e.options[:12],
                "instruction": "Ask the user which one. Do not choose."}
    except resolve.NotFound as e:
        # DETERMINISTIC REPAIR. The commonest routing mistake is a customer name
        # landing in the part-number slot ("% complete for keysight" ->
        # model_status(assembly="keysight")). The resolver already knows that
        # string is a workcell, so say which tool to call instead of returning a
        # dead end and hoping an 8B model works it out. Measured: this is the one
        # question class that survived every prompt change.
        if e.kind == "model":
            try:
                wc = resolve.workcell(e.given)
            except resolve.Ambiguous as amb:
                # Not a model, and more than one workcell answers to it. That IS
                # the answer — "arista" is ARISTANETWORKS and
                # ARISTA_NETWORKS_GLACIER, two customers building different
                # models. Returning "no model found" here threw away the one
                # thing the user needed to hear.
                return {"error": "ambiguous", "kind": "workcell", "given": amb.given,
                        "options": amb.options[:12],
                        "instruction": "Ask the user which one. Do not choose."}
            except resolve.NotFound:
                pass
            else:
                # DO the right call, do not describe it. Told "that is a workcell,
                # call workcell_completion instead", llama3.1:8b re-called
                # model_status with the same argument and the turn died. The
                # intent is not ambiguous once the resolver has proven the string
                # is a workcell, so the redirect is deterministic and the model is
                # simply handed the right answer to write up.
                log.info("chat: redirecting %s(assembly=%r) -> workcell_completion(%r)",
                         name, e.given, wc)
                out = workcell_completion(wc)
                out["_redirected_from"] = f"{name}(assembly={e.given!r})"
                out["_note"] = f"{e.given!r} is a workcell, not a model."
                return out
        return {"error": "not_found", "kind": e.kind, "given": e.given, "did_you_mean": e.near,
                "instruction": "Tell the user it was not found and offer the suggestions."}
    except TypeError as e:
        return {"error": "bad_arguments", "detail": str(e), "expected": sorted(allowed)}
    except Exception as e:                       # a broken tool must not kill the chat
        log.exception("chat tool %s failed", name)
        return {"error": "tool_failed", "detail": str(e)[:200]}


if __name__ == "__main__":
    s = schema()
    assert len(s) == len(_REGISTRY)
    assert all(len(t["function"]["description"]) < 130 for t in s), "keep descriptions terse"
    # Invented arguments are dropped, not fatal.
    r = call("list_workcells", {"nonsense": 1, "workcell": "x"})
    assert "workcells" in r, r
    # Unresolvable names come back as data, never as an exception.
    assert call("workcell_completion", {"workcell": "arista"})["error"] == "ambiguous"
    assert call("workcell_completion", {"workcell": "zzzz"})["error"] == "not_found"
    assert call("nope", {})["error"].startswith("no such tool")
    print("tools self-check OK —", len(s), "tools")
