"""
modules/universe/eval/questions.py
──────────────────────────────────
The exam: Faiz's question pool (vault: Universe/Jabil Universe - Question Pool),
in his words, each with programmatic checks on what a good answer DOES — which
views it read, whether its numbers came from a tool result, whether it carried
the caveat the universe carries. Judgement on the analyses is Faiz's; the checks
only catch the mechanical failures.
"""

from __future__ import annotations

import re


def _used(rec, view: str) -> bool:
    return any(view in (c.get("args", {}).get("sql") or "").lower() for c in rec["tool_calls"] if c["name"] == "universe_query")


def _called(rec, tool: str) -> bool:
    return any(c["name"] == tool for c in rec["tool_calls"])


def _mentions(rec, *words) -> bool:
    a = rec["answer"].lower()
    return any(w in a for w in words)


def grounded_numbers(rec) -> bool:
    """Every number of 3+ digits in the answer appears in some tool result.
    An answer that invents a figure fails, whatever else it says."""
    answer_nums = set(re.findall(r"(?<![\d.])\d{3,}(?![\d.])", rec["answer"].replace(",", "")))
    if not answer_nums:
        return True
    blob = " ".join(str(c.get("result_text", "")) for c in rec["tool_calls"]).replace(",", "")
    tool_nums = set(re.findall(r"\d{3,}", blob))
    # also accept numbers that are sums/rounds of tool numbers? no — keep it strict, report misses
    missing = answer_nums - tool_nums
    rec.setdefault("notes", []).append(f"numbers not found in tool results: {sorted(missing)}" if missing else "all numbers grounded")
    return not missing


def answered(rec) -> bool:
    return rec["stopped"] == "answered" and len(rec["answer"].strip()) > 20


QUESTIONS = [
    {"id": 1, "text": "list all workcells",
     "checks": [("read v_workcell", lambda r: _used(r, "v_workcell")),
                ("says which count it is (active / customer / support)", lambda r: _mentions(r, "active", "customer", "support", "inactive"))]},
    {"id": 2, "text": "how many workcells are in p1",
     "checks": [("read v_workcell", lambda r: _used(r, "v_workcell")),
                ("distinguishes physical from governing plant", lambda r: _mentions(r, "physical", "governing", "supervis", "two"))]},
    {"id": 3, "text": "is the current number of bays for workcell KEYSIGHT enough? simulate how many demands would actually make the workcell struggle or break and not meet demand.",
     "checks": [("read demand", lambda r: _used(r, "v_demand")),
                ("read output or cycle time", lambda r: _used(r, "v_units_out_daily") or _used(r, "v_output_daily") or _used(r, "v_cycle_time") or _used(r, "v_route")),
                ("names what it cannot know (bays / capacity authored)", lambda r: _mentions(r, "bay id", "bay ids", "capacity", "authored", "cannot", "not available", "unknown", "not yet"))]},
    {"id": 4, "text": "what are all the steps this model has to go through and where. sort them end to end. model: the KEYSIGHT model with the most units out in the data",
     "checks": [("read v_route", lambda r: _used(r, "v_route")),
                ("found the top model first", lambda r: _used(r, "v_units_out_daily") or _used(r, "v_output_daily")),
                ("ordered by step", lambda r: _mentions(r, "step") and ("step_order" in " ".join((c.get("args", {}).get("sql") or "") for c in r["tool_calls"]).lower())),
                ("says where is blocked (bay ids)", lambda r: _mentions(r, "bay", "where", "station", "not available"))]},
    {"id": 5, "text": "show me the trend of the top KEYSIGHT model's output for the data we have. and generally what is the workcell's output trend",
     "checks": [("read units out", lambda r: _used(r, "v_units_out_daily") or _used(r, "v_output_daily")),
                ("trend words", lambda r: _mentions(r, "trend", "week", "daily", "increas", "decreas", "flat", "stable", "peak"))]},
    {"id": 6, "text": "which process do u think can be improved for the top KEYSIGHT model based on looking at other faster models. give few suggestions.",
     "checks": [("read cycle time or route", lambda r: _used(r, "v_cycle_time") or _used(r, "v_route")),
                ("names a process", lambda r: any(len(re.findall(r"[A-Z]{2,}", r["answer"])) > 0 for _ in [0])),
                ("gives suggestions", lambda r: _mentions(r, "suggest", "improve", "reduce", "balance", "could"))]},
    {"id": 7, "text": "what can we do to improve our yield",
     "checks": [("read v_fpy_daily", lambda r: _used(r, "v_fpy_daily")),
                ("names the worst step(s)", lambda r: _mentions(r, "fpy", "yield", "step")),
                ("says why is unknown (no defect codes)", lambda r: _mentions(r, "defect", "reason", "cause", "why"))]},
    {"id": 8, "text": "knowledge questions: what is uph, what is cycle time, how do you calculate ole, what variables are related to each other",
     "checks": [("used define", lambda r: _called(r, "universe_define")),
                ("OLE formula", lambda r: _mentions(r, "paid hours", "paid-hours", "earned")),
                ("two cycle times", lambda r: _mentions(r, "study", "elapsed", "scan delta", "stopwatch"))]},
    {"id": 9, "text": "what do you think: project the upcoming 3 weeks of demand and output for workcells KEYSIGHT, BECKMAN COULTER and COLLINS",
     "checks": [("read v_demand", lambda r: _used(r, "v_demand")),
                ("read output history", lambda r: _used(r, "v_units_out_daily") or _used(r, "v_output_daily")),
                ("says it is a projection with a caveat", lambda r: _mentions(r, "assum", "caveat", "only", "limited", "13-week", "planner", "projection", "estimate"))]},
]

GENERIC_CHECKS = [("answered", answered), ("numbers grounded in tool results", grounded_numbers)]
