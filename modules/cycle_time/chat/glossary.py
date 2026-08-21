"""
glossary.py  (cycle_time.chat)
──────────────────────────────
The semantic layer: what our words mean, handed to the model as a system prompt.

WHY A GLOSSARY AND NOT JUST GOOD TOOL DESCRIPTIONS
  The tools say what can be fetched. They cannot say that a WORKCELL IS A
  CUSTOMER — which is Jabil vocabulary, not English, and an 8B model reads
  "workcell" as a workstation on a line every single time. Nor that completion is
  measured in demand UNITS, when the same week reads 59.8% by units and 19.2% by
  models. A bot that grabs whichever number it saw first is worse than no bot: it
  is confidently wrong in the exact way nobody double-checks.

PROSE IS WRITTEN. NUMBERS ARE COMPUTED.
  Every hardcoded figure in this repo has gone stale at least once — "~4,100
  models", "7,246 models / 3.6MB", "the top 500 are 88%". A glossary with 4,401
  baked into it becomes a lying glossary the next time the planner sheet lands.
  So concepts live in TEXT below, and every quantity is read from the marts when
  the prompt is built. If a number is in the system prompt, it was true seconds
  ago.

KEEP IT SHORT
  This is prepended to every request on an 8B model. Measured: llama3.1:8b routes
  6/7 correctly on a terse schema, and gets WORSE with verbose tool descriptions —
  it starts inventing arguments. The same applies here. Facts, no essays.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: Concepts. These do not change when the data refreshes.
_CONCEPTS = """\
You answer questions about cycle-time completion for Jabil Penang's IE team.

VOCABULARY — this is Jabil usage, not general English:
- WORKCELL = CUSTOMER. A workcell is the customer-dedicated production
  organisation. It is NEVER a workstation, a station, or a cell on a line.
- MODEL = ASSEMBLY = a part number: digits, letters and dashes.
- A model is identified by (workcell, assembly) TOGETHER. Two workcells can build
  the same part number as two different models with two different routes.

WHAT "COMPLETE" MEANS:
  Every step MES actually ran for a model is named in IEDB AND has a cycle time.

THE SIX VERDICTS, worst first:
- incomplete    ("Missing CT")      in IEDB and timed, but gaps vs what the floor runs
- no_cycle_time ("No cycle time")   the model is in IEDB, nobody has timed it
- not_in_iedb   ("Not in IEDB")     the model is not in IEDB at all
- not_built     ("Not built yet")   MES has no build record yet. WAIT for it.
- cannot_check  ("Cannot be checked") the WORKCELL is not on MES, so no scan will
                                    EVER arrive. Waiting is pointless.
- complete      ("Complete")

TWO SCOPES — always say which one you used:
- DEMAND ("Planned"): what we are building or about to build. The 13-week planner
  sheet plus the MES ~4-week projection. This is the default for "how are we
  doing" questions and is what the 4Q report measures.
- ALL MODELS: every model that exists, including ones nobody has ordered.

TWO WAYS TO COUNT — they differ a lot, so name the one you used:
- by UNITS (demand volume) — the headline number, because volume is concentrated.
- by MODELS (a count of part numbers) — the work-list number.

RULES:
- Pass workcell and assembly names to tools EXACTLY as the user typed them.
  Never expand, correct or complete a name yourself — the tools resolve names and
  will tell you if one is ambiguous. If they do, ask the user which one.
- Never invent a part number. Only use one the user actually gave you.
- Never invent or recompute a number. Use exactly what the tool returned.
- Answer in ONE short sentence. Nothing else — no notes, no restating the
  question, no explaining where the number came from.
- Only mention "by units" or "by models" when the answer IS a completion
  percentage. A material count or a model count is neither.
"""


#: Term -> its definition, VERBATIM the concepts above. Served deterministically
#: for "what does X mean" — exact, instant, and immune to an 8B paraphrase that
#: rounds "cannot_check" into "not checked yet" (they are opposite advices: one
#: says wait, the other says waiting is pointless).
DEFINITIONS: dict[str, str] = {
    "workcell": "A workcell is a CUSTOMER — the customer-dedicated production organisation. It is never a workstation, a station, or a cell on a line.",
    "model": "A model (= assembly) is a part number, identified by (workcell, assembly) together — two workcells can build the same part number as two different models with different routes.",
    "assembly": "An assembly (= model) is a part number, identified by (workcell, assembly) together.",
    "complete": "Complete: every step MES actually ran for the model is named in IEDB and has a cycle time.",
    "incomplete": "Incomplete (\"Missing CT\"): the model is in IEDB and timed, but there are gaps versus what the floor actually runs.",
    "no_cycle_time": "No cycle time: the model is in IEDB but nobody has timed it yet.",
    "not_in_iedb": "Not in IEDB: the model is not in IEDB at all.",
    "not_built": "Not built yet: MES has no build record for it yet — wait for one.",
    "cannot_check": "Cannot be checked: the WORKCELL is not on MES, so no scan will ever arrive. Waiting is pointless — this is different from not_built, where waiting works.",
    "demand": "Demand (\"Planned\") scope: what we are building or about to build — the 13-week planner sheet plus the MES ~4-week projection. The default scope for \"how are we doing\".",
    "scope": "Two scopes: DEMAND (planned — what we are building) and ALL MODELS (every model that exists, including ones nobody ordered). Always say which one a number used.",
    "coverage": "Coverage: the share of a model's MES steps that are named in IEDB with a cycle time — present steps over expected steps.",
    "unmapped": "Unmapped: a MES step nobody has mapped to an IEDB process yet, so it cannot be checked for a cycle time.",
    "iedb": "IEDB is the IE database holding routes and cycle times — the system this module measures MES reality against.",
    "mes": "MES is the shop-floor execution system — what the floor actually runs and scans. It is the ground truth for which steps a model really has.",
}


def define(question: str) -> str | None:
    """The deterministic answer to a definition question, or None.

    Longest term wins ("no cycle time" before "cycle"); exactly-one-match wins
    ("what is a workcell" -> workcell). More than one distinct hit means the
    question is really a comparison, and the model + full glossary handles it.
    """
    import re
    q = " " + re.sub(r"[^a-z0-9]+", " ", question.lower()) + " "
    hits = [t for t in DEFINITIONS if f" {t.replace('_', ' ')} " in q]
    hits = [t for t in hits if not any(t != o and t.replace("_", " ") in o.replace("_", " ") for o in hits)]
    return DEFINITIONS[hits[0]] if len(hits) == 1 else None


def _live_facts() -> str:
    """The quantities, read fresh. Never hardcoded — see the module docstring.

    Degrades to nothing rather than raising: a chatbot that will not start
    because one mart is rebuilding is worse than one without a headline number.
    """
    from datetime import date
    today = date.today()
    header = f"TODAY is {today.isoformat()} ({today.strftime('%A')}).\n"
    try:
        import pandas as pd
        from api.routers.cycle_time import _completion_demand, _completion_demand_key
        d = pd.DataFrame(_completion_demand(_completion_demand_key())["models"])
        dem = d[d["has_demand"]] if "has_demand" in d else d
        wc = sorted(w for w in dem["customer"].dropna().astype(str).unique() if w.strip("- "))
        return (
            "\n" + header
            + f"RIGHT NOW: {len(dem):,} models are in demand, out of {len(d):,} that exist.\n"
            f"Workcells with demand ({len(wc)}): {', '.join(wc)}.\n"
        )
    except Exception as e:                       # never block a question over this
        log.warning("glossary: live facts unavailable (%s)", e)
        return "\n" + header


def system_prompt() -> str:
    """Concepts + today's numbers. Built per request; the marts are cached, so the
    cost is a dataframe filter, not a parquet read."""
    return _CONCEPTS + _live_facts()


if __name__ == "__main__":
    assert "CUSTOMER" in define("what is a workcell")
    assert "Waiting is pointless" in define("what does cannot_check mean")
    assert define("what does no cycle time mean") == DEFINITIONS["no_cycle_time"]
    assert define("difference between not_built and cannot_check") is None  # two hits
    assert define("what is the meaning of life") is None
    p = system_prompt()
    assert "WORKCELL = CUSTOMER" in p
    assert "cannot_check" in p and "not_built" in p
    # The live half must not be silently empty in a healthy checkout.
    print(p)
    print(f"\n--- {len(p)} chars, ~{len(p)//4} tokens ---")
