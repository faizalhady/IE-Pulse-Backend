"""
mes_route.py  (cycle_time)
──────────────────────────
Fetch a model's CONFIGURED route steps from MES — the shopfloor "truth" the
completion-status feature (P3) compares IEDB cycle-time coverage against.

Two-call chain. Both need a '%' wildcard where the docs/postman show "" — a blank
param raises a 500 'Invalid fmaRoute' / 'Invalid step name':
  1. Assembly/ListAssemblyRouteByAssembly(assemId, fmaRoute='%') → the model's route(s)
  2. Route/ListRouteStepByFactoryMARoute(fmaRouteId, step='%')   → that route's steps

Gotcha for P3: on this server StepType and WorkCenter_ID come back 0, so step
filtering must key on StepName, not type/workcenter.
"""

import logging

from modules.cycle_time.mes_webapi import post, MESWebApiError

log = logging.getLogger(__name__)

_WILDCARD = "%"   # MES rejects a blank fmaRoute/step; '%' = SQL LIKE "all"


def get_assembly_routes(assembly_id) -> list[dict]:
    """The model's configured route(s): [{fma_route_id, factory, ma, route_name}]."""
    rows = post("Assembly", "ListAssemblyRouteByAssembly",
                {"assemId": str(assembly_id), "fmaRoute": _WILDCARD, "langId": "0"})
    return [{
        "fma_route_id": r.get("FactoryMARoute_ID"),
        "factory": r.get("FactoryName"),
        "ma": r.get("ManufacturingAreaName"),
        "route_name": r.get("RouteName"),
    } for r in rows if r.get("FactoryMARoute_ID") is not None]


def get_route_steps(fma_route_id) -> list[dict]:
    """Ordered steps of one route: [{route_step_id, step, order, occurrence}]."""
    rows = post("Route", "ListRouteStepByFactoryMARoute",
                {"fmaRouteId": str(fma_route_id), "step": _WILDCARD, "langId": "0"})
    steps = [{
        "route_step_id": s.get("RouteStep_ID"),
        "step": (s.get("StepName") or "").strip(),
        "order": s.get("StepOrder"),
        "occurrence": s.get("Occurrence"),
    } for s in rows]
    steps.sort(key=lambda x: (x["order"] or 0))
    return steps


def get_model_route_steps(assembly_id) -> list[dict]:
    """All route steps for a model across its route(s), each tagged with its route.
    Raw (no filtering/dedup — that's P3's job). Returns [] if the model has no route."""
    out = []
    for rt in get_assembly_routes(assembly_id):
        try:
            for st in get_route_steps(rt["fma_route_id"]):
                out.append({**st, "route_name": rt["route_name"], "fma_route_id": rt["fma_route_id"]})
        except MESWebApiError as e:
            log.warning("  route steps failed for fma=%s: %s", rt["fma_route_id"], e)
    return out


if __name__ == "__main__":
    # Live smoke test — needs MES_WEBAPI_KEY + VPN.  usage: python -m modules.cycle_time.mes_route <assembly_id>
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    if len(sys.argv) < 2:
        print("usage: python -m modules.cycle_time.mes_route <assembly_id>")
        sys.exit(1)
    aid = sys.argv[1]
    routes = get_assembly_routes(aid)
    print(f"{len(routes)} route(s):", [r["route_name"] for r in routes])
    steps = get_model_route_steps(aid)
    distinct = sorted({s["step"] for s in steps})
    print(f"{len(steps)} total steps, {len(distinct)} distinct names")
    print("distinct step names:", distinct)
