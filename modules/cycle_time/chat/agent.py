"""
agent.py  (cycle_time.chat)
───────────────────────────
The loop: question -> lane -> (maybe a tool) -> answer from what came back.

THREE LANES, DECIDED BY router.py
  instant    greetings/thanks/help. Code, zero model calls. Was 9.5s, is ~0.
  general    not about our data. One plain chat call, and the payload says
             grounded:false — the UI must not dress a memory answer up as a
             sourced one.
  cycletime  routed to ONE tool by a structured call (a fixed form the model
             fills under a grammar — it cannot invent an intent or emit prose),
             then the tool runs, then a second call writes the sentence.

TWO PROMPTS, ONE MODEL — AND THAT ORDER MATTERS
  Routing and writing want opposite prompts, and doing both in one call makes
  both worse (measured on llama3.1:8b: the full glossary attached to routing
  degraded it to answering the wrong question and echoing tool JSON). So: route
  under router._route_prompt() with a forced schema, then write the sentence
  under the glossary with NO tools attached. One model, loaded once and pinned
  (keep_alive), two short calls.

THE MODEL ROUTES AND WRITES. IT DOES NOT COMPUTE, AND IT DOES NOT LOOK UP.
  Every number in a grounded answer came from a mart the frontend also reads.
  resolve.py decides what names mean; tools.py computes; the model fills a form
  and phrases a sentence. That split is what makes an 8B model on a laptop GPU
  trustworthy here.

ERRORS ARE ANSWERS, NOT ROUNDS
  A tool that answers with a question ("arista" is two workcells — which?) or a
  miss (not found, here are the closest) already produced exactly the right
  text deterministically. The old design fed errors back to the model for
  another round; the model then guessed, and a wrong guess reads exactly like a
  right answer. No more rounds: deterministic repair happens in tools.call()
  (workcell-in-model-slot is auto-redirected) and router.repair(); whatever
  still comes back as an error IS the reply.

EVERY ANSWER SHOWS ITS SOURCE
  `sources`, `calls`, `lane`, `intent`, `grounded` ride back with the text, and
  every turn is appended to logs/chat_turns.jsonl — real questions are the eval
  set we do not have to invent.
"""

from __future__ import annotations

import json
import logging
import time

from modules.cycle_time.chat import glossary, ollama, router, tools

log = logging.getLogger(__name__)

#: Guard against a model that answers with a wall of text on a 1-line question.
_MAX_ANSWER_CHARS = 1200

#: The general lane's whole personality. Short on purpose.
_GENERAL_PROMPT = (
    "You are the IE-Pulse assistant for Jabil Penang's IE team. The user's "
    "message is not about cycle-time data. Reply helpfully in one or two short "
    "sentences. If they might actually want data, tell them to name a workcell "
    "or a model part number."
)


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


def _compose(question: str, results: list[dict], routed_text: str,
             on_delta=None) -> str:
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
    messages = [
        {"role": "system", "content": glossary.system_prompt()},
        {"role": "user", "content":
            f"Question: {question}\n\nData:\n{payload}\n\n"
            "Reply with one short sentence answering the question from this data. "
            "No preamble, no notes, no explanation of what you did."},
    ]
    try:
        text = _generate(messages, on_delta)
    except ollama.OllamaError as e:
        log.warning("chat: compose failed (%s) - falling back to raw data", e)
        return _fallback(results)
    # A reply that leaked a tool call or gave up is worse than the plain data.
    if not text or '"name"' in text or "{" in text[:40]:
        return _fallback(results)
    return _strip_meta(text)


def _generate(messages: list[dict], on_delta=None) -> str:
    """One generation, streamed to `on_delta` when a listener exists. The full
    text is still returned and still post-processed — the stream is a preview,
    the final payload is the answer."""
    if on_delta is None:
        return (ollama.chat(messages).get("content") or "").strip()
    parts = []
    for piece in ollama.chat_stream(messages):
        parts.append(piece)
        on_delta(piece)
    return "".join(parts).strip()


def _general(question: str, history: list[dict] | None, system: str,
             on_delta=None) -> str:
    """One plain chat call — the general lane, and the concept lane when the
    system prompt is the glossary."""
    messages = [{"role": "system", "content": system}]
    for h in (history or [])[-6:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        if h.get("content"):
            messages.append({"role": role, "content": str(h["content"])[:1500]})
    messages.append({"role": "user", "content": question})
    return _strip_meta(_generate(messages, on_delta)) or \
        "I did not catch that — ask me about a workcell or a model."


def _log_turn(question: str, out: dict) -> None:
    """One jsonl line per turn. Real usage is the eval set nobody has to invent.
    Never fatal — a full disk must not take the chat down."""
    try:
        from modules.cycle_time.config import BASE_DIR
        p = BASE_DIR / "logs" / "chat_turns.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "q": question[:300], "lane": out.get("lane"),
                "intent": out.get("intent"), "grounded": out.get("grounded"),
                "ok": not out.get("error"), "elapsed_s": out.get("elapsed_s"),
            }, ensure_ascii=False) + "\n")
    except Exception:                            # noqa: BLE001
        log.debug("chat: turn log write failed", exc_info=True)


def ask(question: str, history: list[dict] | None = None,
        on_event=None) -> dict:
    """Answer one question.

    -> {answer, lane, intent, grounded, calls:[{tool, args, ok}], sources:[str],
        model, elapsed_s, error?}

    `on_event(kind, text)` — optional live listener for the SSE endpoint:
    kind "stage" is a progress label ("routing…"), kind "delta" is the next
    piece of a streamed sentence. The return value is identical either way;
    the stream is a preview, the final payload is the answer.
    """
    t0 = time.time()
    emit = on_event or (lambda kind, text: None)
    delta = (lambda t: emit("delta", t)) if on_event else None

    def done(answer: str, lane: str, *, intent: str = "none", grounded: bool = False,
             calls: list | None = None, sources: list | None = None,
             error: str | None = None) -> dict:
        out = {"answer": answer[:_MAX_ANSWER_CHARS].strip(), "lane": lane,
               "intent": intent, "grounded": grounded,
               "calls": calls or [], "sources": sources or [],
               "model": ollama.OLLAMA_MODEL, "elapsed_s": round(time.time() - t0, 1)}
        if error:
            out["error"] = error
        _log_turn(question, out)
        return out

    # L0 — before the availability check on purpose: a greeting deserves a
    # greeting even while Ollama is down.
    canned = router.instant(question)
    if canned:
        return done(canned, "instant")

    ok, detail = ollama.available()
    if not ok:
        return done(f"The local model is not available. {detail}", "error",
                    error="ollama_unavailable")

    # L1 — one structured call. Degrades to a lexicon guess, never raises.
    emit("stage", "routing…")
    r = router.repair(router.route(question, history), question)

    if r["domain"] == "general":
        try:
            emit("stage", "writing…")
            return done(_general(question, history, _GENERAL_PROMPT, delta), "general")
        except ollama.OllamaError as e:
            return done(f"The local model failed: {e}", "error", error="ollama_failed")

    if r["intent"] == "none":
        # In-domain but no tool answers it — usually a concept question ("what
        # does cannot_check mean"). The glossary IS that answer's source, and
        # when exactly one known term is named the definition is served
        # verbatim: instant, exact, and immune to paraphrase.
        exact = glossary.define(question)
        if exact:
            return done(exact, "cycletime", grounded=True, sources=["glossary"])
        try:
            emit("stage", "writing…")
            return done(_general(question, history, glossary.system_prompt(), delta),
                        "cycletime", sources=["glossary"])
        except ollama.OllamaError as e:
            return done(f"The local model failed: {e}", "error", error="ollama_failed")

    if r["intent"] == "open_query":
        from modules.cycle_time.chat import sqllane
        emit("stage", "writing a query…")
        result = sqllane.run(question)
        calls = [{"tool": "open_query", "args": {}, "ok": "error" not in result}]
        if result.get("error"):
            out = done("I could not build a safe query for that — try naming a "
                       "workcell, a status, or a measure from the data.",
                       "cycletime", intent="open_query", calls=calls)
        else:
            # One number gets a sentence; a table is rendered deterministically —
            # an 8B model paraphrasing 10 rows WILL misquote one eventually.
            if result["row_count"] == 1 and len(result["columns"]) == 1:
                text = _compose(question, [result], "")
            else:
                text = sqllane.render(result)
            out = done(text, "cycletime", intent="open_query", grounded=True,
                       calls=calls, sources=[result["_src"]])
        if result.get("sql"):
            out["sql"] = result["sql"]
        return out

    args = router.tool_args(r)
    emit("stage", "reading the mart…")
    result = tools.call(r["intent"], args)
    calls = [{"tool": r["intent"], "args": args, "ok": "error" not in result}]
    log.info("chat: %s(%s) -> %s", r["intent"], args,
             "error:" + str(result.get("error")) if "error" in result else "ok")

    if result.get("error"):
        # Deterministic text (which-one / not-found / suggestions) IS the reply.
        return done(_fallback([result]), "cycletime", intent=r["intent"], calls=calls)

    emit("stage", "writing…")
    answer = _compose(question, [result], "", delta)
    sources = [result["_src"]] if result.get("_src") else []
    return done(answer, "cycletime", intent=r["intent"], grounded=True,
                calls=calls, sources=sources)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import sys
    qs = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else [
        "hello",                                       # instant, 0 model calls
        "how many % complete for keysight",
        "how complete is arista",                      # ambiguous on purpose
        "what is the capital of france",               # general lane
        "show me the bom for PCA-01156-15",
    ]
    for q in qs:
        r = ask(q)
        print(f"\nQ: {q}\nA: {r['answer']}")
        print(f"   lane={r['lane']} intent={r['intent']} grounded={r['grounded']} "
              f"calls={[c['tool'] for c in r['calls']]}  {r['elapsed_s']}s")
        if r["sources"]:
            print(f"   sources: {'; '.join(r['sources'])}")
        # The greeting is the regression this list exists to catch.
        if q == "hello":
            assert r["lane"] == "instant" and not r["calls"] and r["elapsed_s"] < 1, r
    print("\nagent smoke OK — run modules.cycle_time.chat.eval for the full set")
