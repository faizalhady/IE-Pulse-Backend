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

import json
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
    # "4 314" / "4\u202f314" — a space as thousands separator is one number
    text = re.sub(r"(?<=\d)[\s\u202f\u00a0](?=\d{3}\b)", "", rec["answer"]).replace(",", "")
    # digits glued to letters or hyphens are identifiers (N1092-63016), not figures
    answer_nums = set(re.findall(r"(?<![\w.\-])\d{3,}(?![\w.\-])", text))
    answer_nums -= {str(y) for y in range(2019, 2032)}          # years are not figures
    # "~5,000", "about 800", "≈2,300" are estimates by declaration — analysis language, not invention
    answer_nums -= set(re.findall(r"(?:~|≈|about |around |approx\w* |roughly |circa )\s*(\d{3,})", text, re.I))
    answer_nums -= {"100", "3600", "1440", "168"}                # 100%, seconds per hour, minutes per day, hours per week
    if not answer_nums:
        return True
    blob = " ".join(str(c.get("result_text", "")) for c in rec["tool_calls"]).replace(",", "")
    tool_nums = set(re.findall(r"\d{3,}", blob))
    # a total the model added up from one result column (37 + 66 + … = 111) is grounded too
    for c in rec["tool_calls"]:
        try:
            rows = json.loads(c.get("result_text") or "{}").get("rows") or []
        except (ValueError, AttributeError):
            continue
        for col in (rows[0].keys() if rows and isinstance(rows[0], dict) else []):
            vals = [r.get(col) for r in rows if isinstance(r.get(col), (int, float)) and not isinstance(r.get(col), bool)]
            if vals:
                tool_nums.add(str(round(sum(vals))))
    # a round figure within 10% of a tool number is that number, rounded (4,771 → "4,800")
    def rounded_hit(n: str) -> bool:
        v = int(n)
        return n.endswith("0") and any(abs(v - int(t)) <= 0.1 * int(t) for t in tool_nums if t.isdigit())
    # a ratio of two tool numbers (101,559 s ÷ 5 units = 20,312 s/unit) is arithmetic, not invention
    ints = sorted({int(t) for t in tool_nums if t.isdigit()} | {int(t) for t in re.findall(r"\d{1,2}", blob)})
    def ratio_hit(n: str) -> bool:
        v = int(n)
        return any(b and abs(a / b - v) <= max(1.0, 0.005 * v) for a in ints if a >= v for b in ints if 0 < b <= a // max(v, 1) + 1)
    missing = {n for n in answer_nums - tool_nums if not rounded_hit(n) and not ratio_hit(n)}
    rec.setdefault("notes", []).append(f"numbers not found in tool results: {sorted(missing)}" if missing else "all numbers grounded")
    return not missing


def _sql_has(rec, view: str, needle: str) -> bool:
    """Some query over `view` contains `needle` — e.g. the workcell filter the question named."""
    return any(view in (c.get("args", {}).get("sql") or "").lower() and needle.lower() in (c.get("args", {}).get("sql") or "").lower()
               for c in rec["tool_calls"] if c["name"] == "universe_query")


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
                ("kept the KEYSIGHT filter when picking the model", lambda r: any(_sql_has(r, v, n) for v in ("v_units_out_daily", "v_output_daily") for n in ("keysight", "workcell_id = 6", "workcell_id=6"))),
                ("ordered by step", lambda r: _mentions(r, "step") and ("step_order" in " ".join((c.get("args", {}).get("sql") or "") for c in r["tool_calls"]).lower())),
                ("says where is blocked (bay ids)", lambda r: _mentions(r, "bay", "where", "station", "not available"))]},
    {"id": 5, "text": "show me the trend of the top KEYSIGHT model's output for the data we have. and generally what is the workcell's output trend",
     "checks": [("read units out", lambda r: _used(r, "v_units_out_daily") or _used(r, "v_output_daily")),
                ("filtered to KEYSIGHT", lambda r: _sql_has(r, "v_units_out_daily", "keysight") or _sql_has(r, "v_output_daily", "keysight")),
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
                ("OLE formula", lambda r: _mentions(r, "paid hours", "paid-hours", "paid_hours", "earned")),
                ("two cycle times", lambda r: _mentions(r, "study", "elapsed", "scan delta", "stopwatch"))]},
    {"id": 9, "text": "what do you think: project the upcoming 3 weeks of demand and output for workcells KEYSIGHT, BECKMAN COULTER and COLLINS",
     "checks": [("read v_demand", lambda r: _used(r, "v_demand")),
                ("read output history", lambda r: _used(r, "v_units_out_daily") or _used(r, "v_output_daily")),
                ("says it is a projection with a caveat", lambda r: _mentions(r, "assum", "caveat", "only", "limited", "13-week", "planner", "projection", "estimate"))]},
]

GENERIC_CHECKS = [("answered", answered), ("numbers grounded in tool results", grounded_numbers)]
