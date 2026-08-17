"""A model must not be graded off a NEIGHBOUR's cycle times.

resolve() falls back to a suffix / front-name match after an exact miss. That is
right for spelling differences and wrong for neighbouring part numbers:
810-495659-106C has no cycle time of its own, matched 810-495659-106A, and was
reported COMPLETE off a route belonging to a different model. 15 LAM RESEARCH
models read that way.

Run: python tests/test_no_borrowed_route.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.cycle_time import completion_v2 as v2


class FakeCtx:
    """Just enough Ctx for classify(): a catalogue, an IEDB route, a resolver."""

    def __init__(self, catalog_names, timed_route_for):
        self.short = set()
        self.catalog = {"LAMRESEARCH": {v2._anorm(n): n for n in catalog_names}}
        self._catkeys = {c: sorted(d) for c, d in self.catalog.items()}
        self._timed = timed_route_for            # {assembly: [alias codes]}
        # match_step() maps a MES step name to a workbook alias through these.
        self.pmap = {("LAMRESEARCH", " SMT TOP"): "SMTT 1",
                     ("LAMRESEARCH", "SMT BOT"): "SMTB 1",
                     ("LAMRESEARCH", "PACKOUT"): "PACKOUT 1"}
        self.pknown = set(self.pmap)

    # -- the two lookups classify() uses -------------------------------------
    def resolve(self, customer, assembly):
        a = str(assembly).strip()
        if a in self._timed:
            return a                              # exact wins
        # crude stand-in for the suffix/front fallback: same first two segments
        head = "-".join(a.split("-")[:2])
        for k in sorted(self._timed):
            if "-".join(k.split("-")[:2]) == head:
                return k
        return None

    def in_catalog(self, cn, assembly):
        return v2._anorm(str(assembly).strip()) in self.catalog.get(cn, {})

    in_catalog_exact = in_catalog

    def near(self, cn, assembly):
        return ""

    def iedb(self, customer):
        return {a: {"any_ct": True, "ct_codes": set(codes), "all_codes": set(codes),
                    "ct_names": {}, "all_names": {}, "detail": []}
                for a, codes in self._timed.items()}


def main() -> None:
    # IEDB lists BOTH names. Only -106A has cycle times.
    ctx = FakeCtx(catalog_names=["810-495659-106A", "810-495659-106C"],
                  timed_route_for={"810-495659-106A": ["SMTT 1", "SMTB 1", "PACKOUT 1"]})
    mes = [([" SMT TOP"], 1, 1), (["SMT BOT"], 2, 1), (["PACKOUT"], 3, 1)]

    # -- the model that HAS its own cycle times still grades normally ---------
    r, _ = v2.classify(ctx, "LAMRESEARCH", "810-495659-106A", mes, "batch")
    assert r["status"] != "no_cycle_time", f"-106A owns its route, got {r['status']}"

    # -- the neighbour must NOT be graded off it -----------------------------
    r, rows = v2.classify(ctx, "LAMRESEARCH", "810-495659-106C", mes, "batch")
    assert r["status"] == "no_cycle_time", \
        f"-106C has no cycle time of its own; expected no_cycle_time, got {r['status']}"
    assert r["reason"] == "in_iedb_untimed", f"expected in_iedb_untimed, got {r['reason']}"
    assert r["status"] != "complete", "NEVER complete off a neighbour's route"
    # The MES route is still returned, or the drawer shows an empty left pane for
    # exactly the models where knowing what the floor runs matters most.
    assert len(rows) == len(mes), f"MES route must survive, got {len(rows)} of {len(mes)}"

    # -- a name IEDB does NOT list keeps the old fallback ---------------------
    # (spelling differences are why resolve() has a fallback at all)
    ctx2 = FakeCtx(catalog_names=["810-495659-106A"],
                   timed_route_for={"810-495659-106A": ["SMTT 1", "SMTB 1", "PACKOUT 1"]})
    r, _ = v2.classify(ctx2, "LAMRESEARCH", "810-495659-106C", mes, "batch")
    assert r["status"] != "no_cycle_time", \
        "not in the catalogue -> fallback still allowed, else spelling variants break"

    print("test_no_borrowed_route: all assertions passed")


if __name__ == "__main__":
    main()
