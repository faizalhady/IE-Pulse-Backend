"""
router.py  (cycle_time.chat)
────────────────────────────
Decide what a message IS before any model writes an answer. Three layers,
cheapest first, and each layer only exists because the one above it cannot do
the job:

  L0  INSTANT — a closed set of greetings/thanks/help. Code, 0 model calls.
      "hello" used to route to list_workcells() and answer "There are 34
      workcells with demand" after 9.5s. A greeting is not a routing problem.

  L1  ONE STRUCTURED CALL — the model fills a fixed form (domain, intent,
      slots) under Ollama's `format` schema. Grammar-constrained: it cannot
      invent an intent, cannot emit prose, cannot skip a field. This replaces
      free-form tool-calling, which is where every measured failure lived —
      leaked tool JSON, invented argument names, "NO tool" parroted as an
      answer.

  LEXICON OVERRIDE — deterministic check AFTER the model: if it said "general"
      but the message names a workcell or a part number, code flips it back to
      cycletime. The model advises; the lexicon decides. Only that direction —
      a false "cycletime" still lands somewhere safe (concept lane), a false
      "general" would answer a data question from an 8B model's memory.

The model never decides what a name MEANS (resolve.py does) and never computes
a number (tools.py does). Here it does the one remaining judgment call —
"which question is this" — inside a form it cannot break out of.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

from modules.cycle_time.chat import ollama, tools

log = logging.getLogger(__name__)

# ─── L0: the instant lane ────────────────────────────────────────────────────

#: Whole-message matches after normalisation. Closed set on purpose: a prefix
#: match would swallow "hello, how complete is keysight". Exact or nothing.
_GREET = {"hello", "hi", "hey", "yo", "hai", "helo", "good morning",
          "good afternoon", "good evening", "morning", "how are you",
          "whats up", "what's up", "sup"}
_THANKS = {"thanks", "thank you", "tq", "ty", "thx", "terima kasih",
           "thanks a lot", "nice", "great", "ok thanks", "okay thanks"}
_BYE = {"bye", "goodbye", "see you", "later", "good night"}
_HELP = {"help", "who are you", "what are you", "what can you do",
         "what can i ask", "what can i ask you", "how do i use this",
         "what do you do"}

_GREET_REPLY = ("Hi! Ask me about cycle-time completion — a workcell, a model, "
                "a status, or the plant overall.")
_THANKS_REPLY = "Anytime."
_BYE_REPLY = "Bye!"


def _capabilities() -> str:
    """Built from the registry, so a new tool advertises itself."""
    lines = [f"- {desc}" for _, desc, _ in tools._REGISTRY]
    return ("I answer questions about cycle-time completion from the same data "
            "the screens read. You can ask for:\n" + "\n".join(lines)
            + "\nName workcells and part numbers exactly as you know them — "
              "I will ask if one is ambiguous.")


def instant(question: str) -> str | None:
    """The canned reply, or None if this deserves real routing."""
    q = re.sub(r"[^a-z' ]+", " ", question.lower()).strip()
    q = re.sub(r"\s+", " ", q)
    if q in _GREET:
        return _GREET_REPLY
    if q in _THANKS:
        return _THANKS_REPLY
    if q in _BYE:
        return _BYE_REPLY
    if q in _HELP:
        return _capabilities()
    return None


# ─── the lexicon ─────────────────────────────────────────────────────────────

#: Words that only appear when someone is asking about OUR data. Deliberately
#: specific — "model" or "complete" alone would drag ordinary English in.
_DOMAIN_WORDS = {"workcell", "workcells", "iedb", "mes", "bom", "cycle time",
                 "cycletime", "completion", "assembly", "assemblies", "demand",
                 "unmapped", "not in iedb", "no cycle time", "incomplete",
                 "cannot check", "not built", "verdict", "planner", "4q"}

#: A part number: contains a digit, contains a dash, reasonably long.
#: PCA-01247-01, 853-111462-068A, B1500A-ATO-42719.
_PART_RE = re.compile(r"\b(?=[A-Za-z0-9-]*\d)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b")


@lru_cache(maxsize=1)
def _workcell_keys() -> tuple[str, ...]:
    from modules.cycle_time.chat import resolve
    from modules.cycle_time.model_universe import norm
    return tuple(norm(w) for w in resolve._workcells())


def looks_domain(question: str) -> bool:
    """Deterministic evidence the message is about our data."""
    # Underscores become spaces so "cannot_check" hits the phrase list.
    ql = question.lower().replace("_", " ")
    if any(w in ql for w in _DOMAIN_WORDS):
        return True
    if _PART_RE.search(question) and len(_PART_RE.search(question).group()) >= 6:
        return True
    qn = re.sub(r"[^A-Z0-9]", "", question.upper())
    all_toks = re.split(r"[^A-Za-z0-9]+", question.upper())
    long_toks = [t for t in all_toks if len(t) >= 5]
    for wk in _workcell_keys():
        # Substring only for names long enough not to hide inside English —
        # the workcell "GO" matched "translate GOod morning" and dragged a
        # translation request into the data lane.
        if len(wk) >= 5 and wk in qn:
            return True
        # Short names (GO, BD, LTX, TMO...) must stand alone as a word.
        if len(wk) < 5 and wk in all_toks:
            return True
        if any(wk.startswith(t) for t in long_toks):
            return True
    return False


#: A definition question: "what does X mean", "what is a X", "define X".
#: Deliberately narrow — "what is THE % complete for keysight" has no article
#: and must keep going to a tool.
_CONCEPT_RE = re.compile(
    r"^\s*(what\s+does\s+.+\s+mean|what\s+is\s+(a|an)\s+\S+|define\s+\S+|"
    r"meaning\s+of\s+\S+)\s*\??\s*$", re.IGNORECASE)


def concept_question(question: str) -> bool:
    """A domain definition question — answered from the glossary, no tool, and
    no routing call: the two eval misses were the model picking model_status for
    "what does cannot_check mean" and list_workcells for "what is a workcell".
    A definition has a grammar; grammar is code's job."""
    return bool(_CONCEPT_RE.match(question)) and looks_domain(question)


# ─── L1: the structured routing call ─────────────────────────────────────────

INTENTS = tuple(tools.FUNCS)                       # the 9 tool names

_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "enum": ["cycletime", "general"]},
        "intent": {"type": "string", "enum": list(INTENTS) + ["none"]},
        # Slots. "" = the user did not give it. Every field is required so the
        # grammar forces the model to consider each one — an optional field is
        # one a small model simply never fills.
        "workcell": {"type": "string"},
        "assembly": {"type": "string"},
        "status": {"type": "string",
                   "enum": ["incomplete", "no_cycle_time", "not_in_iedb",
                            "not_built", "cannot_check", "complete", ""]},
        "scope": {"type": "string", "enum": ["demand", "all", ""]},
        "query": {"type": "string"},
    },
    "required": ["domain", "intent", "workcell", "assembly",
                 "status", "scope", "query"],
}

#: Terse on purpose — the measured pattern on this model is that every added
#: sentence costs routing accuracy. The intent menu reuses the registry's
#: one-liners so the router and the tools cannot describe a tool differently.
def _route_prompt() -> str:
    menu = "\n".join(f"- {fn.__name__}: {desc}" for fn, desc, _ in tools._REGISTRY)
    return (
        "Classify the user's message about manufacturing cycle-time data.\n"
        "domain: 'cycletime' if it asks about our data — workcells (customers), "
        "models/assemblies (part numbers), completion %, statuses, cycle times, "
        "BOM, demand, trends. Otherwise 'general'.\n"
        "intent: the ONE tool that answers it, or 'none'.\n" + menu + "\n"
        "Fill slots with values COPIED EXACTLY from the message; leave '' when "
        "not given. Never correct or complete a name."
    )


def route(question: str, history: list[dict] | None = None) -> dict:
    """-> {domain, intent, workcell, assembly, status, scope, query}

    Never raises on a bad model reply — a routing failure degrades to
    {domain: cycletime|general by lexicon, intent: none}, which the agent turns
    into a glossary/general answer rather than a dead turn.
    """
    if concept_question(question):
        return {"domain": "cycletime", "intent": "none", "workcell": "",
                "assembly": "", "status": "", "scope": "", "query": ""}

    messages = [{"role": "system", "content": _route_prompt()}]
    for h in (history or [])[-4:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        if h.get("content"):
            messages.append({"role": role, "content": str(h["content"])[:500]})
    messages.append({"role": "user", "content": question})

    fallback = {"domain": "cycletime" if looks_domain(question) else "general",
                "intent": "none", "workcell": "", "assembly": "",
                "status": "", "scope": "", "query": ""}
    try:
        msg = ollama.chat(messages, format=_SCHEMA)
        import json
        r = json.loads(msg.get("content") or "{}")
    except Exception as e:                      # noqa: BLE001 — degrade, never die
        log.warning("chat route failed (%s) — lexicon fallback", e)
        return fallback
    if not isinstance(r, dict):
        return fallback

    out = {k: str(r.get(k) or "").strip() for k in
           ("domain", "intent", "workcell", "assembly", "status", "scope", "query")}
    if out["domain"] not in ("cycletime", "general"):
        out["domain"] = fallback["domain"]
    if out["intent"] not in INTENTS:
        out["intent"] = "none"

    # THE OVERRIDE. A workcell name or part number in the message is proof this
    # is a data question, whatever the model said. One direction only.
    if out["domain"] == "general" and looks_domain(question):
        log.info("chat route: lexicon override general -> cycletime for %r", question[:60])
        out["domain"] = "cycletime"
    # The same evidence, both directions: cycletime WITHOUT a tool and WITHOUT
    # any domain word/name in the message is the model guessing — "translate
    # good morning to malay" landed here. General is the safe lane; its prompt
    # still redirects anyone who actually wanted data.
    if out["domain"] == "cycletime" and out["intent"] == "none"             and not looks_domain(question):
        log.info("chat route: downgrade cycletime/none -> general for %r", question[:60])
        out["domain"] = "general"
    if out["domain"] == "general":
        out["intent"] = "none"
    return out


# ─── slots -> tool arguments ─────────────────────────────────────────────────

def tool_args(r: dict) -> dict:
    """Explicit per-intent mapping. tools.call() would strip strays anyway, but
    an explicit map means an empty required slot is repaired HERE, visibly,
    instead of dying inside the tool as a TypeError."""
    wc, asm, q = r["workcell"], r["assembly"] or r["query"], r["query"] or r["assembly"]
    intent = r["intent"]
    if intent in ("model_status", "model_bom", "model_cycle_time"):
        if not asm:                              # model tool with no model named
            return {}                            # caller downgrades the intent
        return {"workcell": wc, "assembly": asm}
    if intent == "workcell_completion":
        return {"workcell": wc, **({"scope": r["scope"]} if r["scope"] else {})}
    if intent == "plant_completion":
        return {"scope": r["scope"]} if r["scope"] else {}
    if intent == "models_by_status":
        return {"workcell": wc, "status": r["status"]}
    if intent == "search_models":
        return {"query": q}
    if intent == "completion_trend":
        return {"workcell": wc}
    return {}                                    # list_workcells / none


def repair(r: dict) -> dict:
    """Slot sanity BEFORE dispatch — each rule is a measured 8B habit, fixed in
    code where fixing the prompt only moved the failure around."""
    intent, wc = r["intent"], r["workcell"]
    asm = r["assembly"] or r["query"]
    if intent in ("model_status", "model_bom", "model_cycle_time") and not asm:
        r["intent"] = "workcell_completion" if wc else "none"
    if intent == "models_by_status" and not r["status"]:
        r["intent"] = "workcell_completion" if wc else "none"
    if intent == "workcell_completion" and not wc:
        r["intent"] = "plant_completion"         # "how complete are we" — the plant
    if intent == "search_models" and not (r["query"] or r["assembly"]):
        r["intent"] = "none"
    return r


if __name__ == "__main__":
    # Offline self-check: L0 and the lexicon, no model needed.
    assert instant("hello") and instant("  TQ! ") and instant("who are you")
    assert instant("hello, how complete is keysight") is None, "prefix must not swallow"
    assert instant("what can you do") and "cycle-time" in _capabilities()
    assert looks_domain("how complete is keysight")
    assert looks_domain("bom for PCA-01247-01")
    assert looks_domain("show me arista please")          # prefix of a workcell
    assert not looks_domain("what is the capital of france")
    assert not looks_domain("translate good morning to malay"), "workcell GO must not substring-match"
    assert looks_domain("what does cannot_check mean")
    assert looks_domain("how many units does go have")            # GO as a word
    assert concept_question("what is a workcell")
    assert concept_question("what does cannot_check mean")
    assert not concept_question("what is the % complete for keysight")
    assert not concept_question("what is a good restaurant")      # not domain
    assert repair({"intent": "model_status", "workcell": "keysight", "assembly": "",
                   "query": "", "status": "", "scope": "", "domain": "cycletime"}
                  )["intent"] == "workcell_completion"
    assert repair({"intent": "workcell_completion", "workcell": "", "assembly": "",
                   "query": "", "status": "", "scope": "", "domain": "cycletime"}
                  )["intent"] == "plant_completion"
    print("router self-check OK —", len(INTENTS), "intents")
