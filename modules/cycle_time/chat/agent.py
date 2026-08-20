"""
agent.py  (cycle_time.chat)
───────────────────────────
The loop: question -> pick a tool -> run it -> answer from what it returned.

TWO PROMPTS, ONE MODEL — AND THAT ORDER MATTERS
  Routing and writing want opposite prompts, and doing both in one call makes both
  worse. Measured on llama3.1:8b:
    - a ONE-LINE system prompt + terse tools ......... 6/7 questions routed right
    - the same tools under the full glossary ......... routing degraded; it began
      answering "which keysight models have no cycle time" with model_status, and
      echoed raw tool-call JSON into its own reply
  A small model treats the system prompt as a pattern to imitate, so a long one
  crowds out the schema. But the glossary is exactly what the ANSWER needs, or the
  reply says "31.7% complete" without saying by models, by units, or in what scope.

  So: route under `_ROUTE_PROMPT` with tools attached, then write the sentence
  under the glossary with NO tools attached. One model, loaded once, two calls.
  A chain of two MODELS would swap 5 GB in and out of an 8 GB card per question.

THE MODEL ROUTES. IT DOES NOT COMPUTE, AND IT DOES NOT LOOK ANYTHING UP.
  Two jobs only: choose a tool, and extract its arguments. Every number in the
  answer came from a mart the frontend also reads. That split is what makes an 8B
  model on a laptop GPU trustworthy for this — it is reliable at routing (measured
  6/7) and would be hopeless at deriving a completion percentage.

WHY THERE IS A SECOND ROUND
  A tool can answer with a QUESTION rather than data: "arista" matches two real
  workcells, so `resolve` refuses to pick and returns the candidates. Feeding that
  back lets the model ask the user which one. Without the second round the only
  possible replies are a wrong guess or a dead end, and the wrong guess is worse
  because it reads exactly like a right one.

  `_MAX_ROUNDS` is 3: route, repair once, answer. A small model given an unbounded
  loop will happily call `list_workcells` forever.

EVERY ANSWER SHOWS ITS SOURCE
  `sources` and `calls` ride back with the text. This is meant to replace IEDB;
  people do not move onto a system whose numbers they cannot check.
"""

from __future__ import annotations

import json
import logging
import time

from modules.cycle_time.chat import glossary, ollama, tools

log = logging.getLogger(__name__)

#: Deliberately tiny. Every sentence added here cost routing accuracy — see the
#: module docstring. The vocabulary lives in the ANSWER prompt, where it helps.
_ROUTE_PROMPT = (
    "You answer questions about manufacturing cycle-time data by calling exactly "
    "one tool. A workcell is a customer name. An assembly is a part number. "
    "Pass names exactly as the user typed them; never complete or correct a name. "
    # ponytail: the ONE sentence that buys an escape hatch. "hello" had no path
    # that did not end in a tool, so it routed to the cheapest tool taking no
    # arguments - list_workcells - and composed "There are 34 workcells with
    # demand." Ceiling: permission to skip a tool is permission the model can
    # misuse on a real question. If routing accuracy drops, the upgrade is a
    # deterministic gate BEFORE the model, not more prompt.
    "If the message is not a question about this data - a greeting, small talk, "
    "or a question about you - call NO tool and reply in one short sentence."
)

#: route -> repair once. See the module docstring.
_MAX_ROUNDS = 3

#: Guard against a model that answers with a wall of text on a 1-line question.
_MAX_ANSWER_CHARS = 1200


def _fallback(results: list[dict]) -> str:
    """Text for when the model called tools and then produced nothing.

    Small models sometimes stop after the tool round. The data is already in hand
    at that point, so returning it plainly beats an apology — and it is the same
    data the sentence would have been built from anyway.
    """
    if not results:
        return ("I could not find that. Try naming a workcell (e.g. KEYSIGHT) "
                "or a model part number.")
    r = results[-1]
    if r.get("error") == "ambiguous":
        return f"{r['given']!r} matches more than one {r['kind']}: {', '.join(r['options'])}. Which one?"
    if r.get("error") == "wrong_tool":
        return r.get("instruction") or "That name is a workcell, not a model."
    if r.get("error") == "not_found":
        near = ", ".join(r.get("did_you_mean") or [])
        return f"No {r['kind']} matching {r['given']!r}." + (f" Did you mean: {near}?" if near else "")
    return "Here is what I found:\n\n```json\n" + json.dumps(r, indent=2, default=str)[:900] + "\n```"


#: Openers a small model uses when it narrates its own instructions instead of
#: answering. Belt-and-braces on top of the shortened compose prompt.
_META = ("note:", "i did not", "i used", "i have used", "based on the data",
         "based on the tool", "according to the tool", "here is", "as requested")


def _strip_meta(text: str) -> str:
    """Drop the model's commentary about itself, keep the answer.

    Prompt wording alone does not stop this — it is a habit, not a
    misunderstanding. Telling it to "use ONLY numbers from this JSON" produced
    "Note: I did not use any external information" printed under the number, which
    makes a sourced figure look doubtful. The real provenance is the tool and mart
    line the UI already renders.
    """
    keep = [ln for ln in text.splitlines()
            if ln.strip() and not ln.strip().lower().startswith(_META)]
    return "\n".join(keep).strip() or text.strip()


def _compose(question: str, results: list[dict], routed_text: str) -> str:
    """Turn the tool output into a sentence, under the glossary and with NO tools
    attached.

    Tools are withheld on purpose: given a schema the model tries to call
    something again and emits tool-call JSON as prose. Withholding them leaves it
    one job — write the sentence — which is what it is good at.

    A tool that answered with a QUESTION (ambiguous name) skips this entirely: the
    deterministic text is already exactly right, and paraphrasing an ambiguity is
    how a model talks itself into picking one.
    """
    if not results:
        return routed_text.strip() or _fallback(results)
    last = results[-1]
    if last.get("error"):
        return _fallback(results)

    # Big arrays are for the UI, not the sentence. A 202-material BOM took the
    # compose call from ~3s to 27s and taught it nothing the count did not.
    slim = {k: (f"[{len(v)} items, omitted]" if isinstance(v, list) and len(v) > 8 else v)
            for k, v in last.items()}
    payload = json.dumps(slim, default=str)[:2500]
    try:
        msg = ollama.chat([
            {"role": "system", "content": glossary.system_prompt()},
            {"role": "user", "content":
                f"Question: {question}\n\nData:\n{payload}\n\n"
                "Reply with one short sentence answering the question from this data. "
                "No preamble, no notes, no explanation of what you did."},
        ])
        text = (msg.get("content") or "").strip()
    except ollama.OllamaError as e:
        log.warning("chat: compose failed (%s) - falling back to raw data", e)
        return _fallback(results)
    # A reply that leaked a tool call or gave up is worse than the plain data.
    if not text or '"name"' in text or "{" in text[:40]:
        return _fallback(results)
    return _strip_meta(text)


def ask(question: str, history: list[dict] | None = None) -> dict:
    """Answer one question.

    -> {answer, calls:[{tool, args, ok}], sources:[str], model, elapsed_s, error?}
    """
    t0 = time.time()
    ok, detail = ollama.available()
    if not ok:
        return {"answer": f"The local model is not available. {detail}",
                "calls": [], "sources": [], "error": "ollama_unavailable",
                "elapsed_s": round(time.time() - t0, 1)}

    messages: list[dict] = [{"role": "system", "content": _ROUTE_PROMPT}]
    # Prior turns carry context ("and its BOM?"), but only the text — replaying
    # old tool payloads would blow an 8k window in three questions.
    for h in (history or [])[-6:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        if h.get("content"):
            messages.append({"role": role, "content": str(h["content"])[:1500]})
    messages.append({"role": "user", "content": question})

    calls: list[dict] = []
    results: list[dict] = []
    schema = tools.schema()

    for rnd in range(_MAX_ROUNDS):
        try:
            msg = ollama.chat(messages, tools=schema)
        except ollama.OllamaError as e:
            return {"answer": f"The local model failed: {e}", "calls": calls,
                    "sources": [], "error": "ollama_failed",
                    "elapsed_s": round(time.time() - t0, 1)}

        wanted = ollama.tool_calls(msg)
        if not wanted:
            answer = _compose(question, results, msg.get("content") or "")
            break

        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": msg.get("tool_calls")})
        for name, args in wanted[:3]:            # one question, not a batch job
            out = tools.call(name, args)
            results.append(out)
            calls.append({"tool": name, "args": args, "ok": "error" not in out})
            log.info("chat: %s(%s) -> %s", name, args,
                     "error:" + str(out.get("error")) if "error" in out else "ok")
            messages.append({"role": "tool", "name": name,
                             "content": json.dumps(out, default=str)[:2000]})

        # A tool that ANSWERED ends the loop. Another round with the schema
        # attached only invites the model to write its own summary — on the BOM
        # question it produced 1,185 wasted characters in 22.4s, which _compose
        # then discarded. Rounds exist to repair an ERROR (ambiguous name, wrong
        # slot), so only an error earns one.
        if results and not results[-1].get("error"):
            answer = _compose(question, results, "")
            break
    else:
        # Ran out of rounds still wanting tools — answer from what we have.
        answer = _compose(question, results, "")

    sources = []
    for r in results:
        s = r.get("_src")
        if s and s not in sources:
            sources.append(s)

    return {"answer": answer[:_MAX_ANSWER_CHARS].strip(),
            "calls": calls, "sources": sources,
            "model": ollama.OLLAMA_MODEL if hasattr(ollama, "OLLAMA_MODEL") else None,
            "elapsed_s": round(time.time() - t0, 1)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import sys
    qs = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else [
        "hello",                                       # must call NO tool
        "how many % complete for keysight",
        "how complete is arista",                      # ambiguous on purpose
        "which keysight models have no cycle time",
        "show me the bom for PCA-01156-15",
    ]
    for q in qs:
        r = ask(q)
        print(f"\nQ: {q}\nA: {r['answer']}")
        print(f"   calls={[c['tool'] for c in r['calls']]}  {r['elapsed_s']}s")
        # The greeting is the regression this list exists to catch: any tool at
        # all here means the escape hatch in _ROUTE_PROMPT stopped working.
        if q == "hello":
            assert not r["calls"], f"'hello' called {[c['tool'] for c in r['calls']]}"
        if r["sources"]:
            print(f"   sources: {'; '.join(r['sources'])}")
