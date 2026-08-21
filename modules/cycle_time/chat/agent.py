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

from modules.cycle_time.chat import glossary, llm, router, tools

log = logging.getLogger(__name__)

#: Guard against a model that answers with a wall of text on a 1-line question.
_MAX_ANSWER_CHARS = 1200

#: The general lane's personality. The brevity clause is swapped per brain —
#: two short sentences muzzles the 8B; the cloud model answers naturally.
def _general_prompt() -> str:
    brevity = ("Reply helpfully and naturally — brief for simple questions, "
               "fuller when the question deserves it."
               if llm.rich() else
               "Reply helpfully in one or two short sentences.")
    return ("You are the IE-Pulse assistant for Jabil Penang's IE team. The "
            f"user's message is not about cycle-time data. {brevity} If they "
            "might actually want data, tell them to name a workcell or a model "
            "part number.")


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
    return _render_result(r)


def _render_result(r: dict) -> str:
    """Tool result -> readable markdown, NO model involved.

    This is the floor of the whole chain — what the user sees when the cloud
    is capped AND the local card is down at once. It used to be a raw ```json
    block, which is exactly what a chatbot must never say out loud. Scalars
    become bold-label lines, the first list of dicts becomes a markdown table
    (the FE renders both)."""
    def human(k: str) -> str:
        return k.replace("_", " ")

    lines, table = [], None
    for k, v in r.items():
        if str(k).startswith("_"):
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            if table is None:
                table = v
            continue
        if isinstance(v, dict):
            # One level of scalars renders inline — the both-scopes payload
            # ({planned: {...}, all_models: {...}}) must not vanish.
            flat = ", ".join(f"{human(str(kk))} {vv}" for kk, vv in v.items()
                             if not isinstance(vv, (dict, list)))
            if flat:
                lines.append(f"**{human(str(k))}** — {flat}")
            continue
        if isinstance(v, list):
            continue                             # non-dict lists: noise
        lines.append(f"**{human(str(k))}:** {v}")
    out = "  \n".join(lines) or "No matching data."
    if table:
        cols = [c for c in table[0] if not str(c).startswith("_")][:6]
        head = "| " + " | ".join(human(str(c)) for c in cols) + " |"
        sep = "|" + " --- |" * len(cols)
        body = "\n".join(
            "| " + " | ".join(str(row.get(c, "") if row.get(c) is not None else "—")
                              for c in cols) + " |"
            for row in table[:20])
        more = f"\n\n_… and {len(table) - 20} more rows_" if len(table) > 20 else ""
        out += f"\n\n{head}\n{sep}\n{body}{more}"
    return out


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

    # The muzzle is SIZED TO THE BRAIN. The local 8B gets one short sentence
    # and 8 rows — anything more and it rambles or misquotes (a 202-material
    # BOM took its compose from ~3s to 27s). A cloud-class model gets the
    # fuller payload and permission to actually answer: depth, comparisons,
    # and EVERY question in the message, not just the first.
    rich = llm.rich()
    cap_items, cap_chars = (40, 12000) if rich else (8, 2500)
    slim = {k: (f"[{len(v)} items, omitted]" if isinstance(v, list) and len(v) > cap_items else v)
            for k, v in last.items()}
    payload = json.dumps(slim, default=str)[:cap_chars]
    # question_result marks a SQL answer whose rows the UI renders as a real
    # table under the prose — restating them is duplication, not depth.
    tabled = bool(last.get("question_result"))
    instruction = (
        ("The rows are ALREADY shown to the user as a table below your text — "
         "do not list or repeat them. Summarise what they show, point out "
         "anything notable, and answer every part of the question the table "
         "itself does not. 1-4 sentences, GitHub-flavored Markdown. "
         if tabled else
         "Answer the question fully from this data — if it asks more than one "
         "thing, answer each part. GitHub-flavored Markdown: sentences for "
         "simple answers; bullets or a compact table when comparing. ")
        + "Use ONLY numbers present in the data, never invent or recompute "
          "one. No preamble, no notes about what you did."
        if rich else
        "Reply with one short sentence answering the question from this data. "
        "No preamble, no notes, no explanation of what you did.")
    messages = [
        {"role": "system", "content": glossary.system_prompt()},
        {"role": "user", "content":
            f"Question: {question}\n\nData:\n{payload}\n\n{instruction}"},
    ]
    try:
        text = _generate(messages, on_delta)
    except llm.LLMError as e:
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
        return (llm.chat(messages).get("content") or "").strip()
    parts = []
    for piece in llm.chat_stream(messages):
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



# ─── the rich agent loop ─────────────────────────────────────────────────────

_RUN_SQL_TOOL = {"type": "function", "function": {
    "name": "run_sql",
    "description": "Run ONE read-only SELECT over the llm facts views "
                   "(llm_model_facts, llm_workcell_facts, llm_process_facts). "
                   "Filter workcells on workcell_key (UPPERCASE alphanumerics).",
    "parameters": {"type": "object",
                   "properties": {"sql": {"type": "string"}},
                   "required": ["sql"]}}}

#: Rounds, not retries: enough to resolve two names, pull three datasets and
#: write — a runaway loop stops here, with whatever it gathered.
_MAX_ROUNDS = 6


def _rich_system() -> str:
    # Identity FIRST, glossary demoted to reference BELOW — order is power in
    # a system prompt. When the glossary led, its opening line ("You answer
    # questions about cycle-time completion...") acted as a scope fence and the
    # model refused "write a story about a knight and dragon with this data" —
    # a legitimate request to dress real numbers in another form.
    from datetime import date
    from modules.cycle_time.chat import facts
    today = date.today()
    return (
        f"TODAY is {today.isoformat()} ({today.strftime('%A')}). Date columns "
        "in the views are ISO TEXT — CAST(col AS DATE) to compare; DuckDB "
        "CURRENT_DATE works.\n"
        "You are the IE-Pulse data ANALYST for Jabil Penang's IE team — and a "
        "capable, willing assistant. Answer ANY reasonable request: lookups, "
        "comparisons, assessments, opinions, multi-part questions, and STYLE "
        "requests (a story, an analogy, explain-like-I'm-new, a summary for a "
        "manager) that present real data in another form — those are "
        "legitimate; use numbers already in this conversation or fetch fresh "
        "ones. Refuse nothing harmless.\n"
        "For every FIGURE you state, call tools — never a number a tool did "
        "not return this turn or in this conversation. Chain tools freely: "
        "resolve both workcells, fetch both, compare. General questions may "
        "be answered directly without tools.\n"
        "NAMES: users typo constantly. When a lookup fails with exactly ONE "
        "suggestion, take it, continue, and note the correction in your answer "
        "('assuming you meant KEYSIGHT'). Only stop to ask when a name is "
        "genuinely AMBIGUOUS (several real matches) or has no close match.\n"
          "BE THOROUGH BY DEFAULT — you are a 120B analyst, not a lookup box. "
          "A bare count is NEVER a full answer: when models are the subject, "
          "LIST them (assembly + the key fact each) and add a line of context "
          "or an insight (what stands out, what to do about it). ALWAYS state "
          "the scope split when it differs — e.g. '12 models in total, 7 of "
          "them Planned'. Default scope is ALL MODELS unless the user says "
          "planned. Only greetings and single-fact questions deserve one "
          "sentence.\n"
          "Broad questions (compare X and Y, tell me about X, analyse X): for "
          "EACH side gather completion, the trend (completion_trend) and "
          "notable models (models_by_status or run_sql), then write the "
          "analysis.\n"
          "FORMAT IN GITHUB-FLAVORED MARKDOWN - it renders. Narrow answer: "
          "plain sentences. Broad answer: **bold** labels or ### headings per "
          "section, bullet lines for findings, a compact md table for a "
          "side-by-side, and end with a one-line **Verdict:**. Never a wall "
          "of prose.\n"
          "QUESTION -> SOURCE map, so you plan chains instead of guessing:\n"
          "- how complete / status counts -> workcell_completion tool or llm_workcell_facts\n"
          "- WHY incomplete, WHICH steps missing/unmapped -> llm_route_steps\n"
          "- longest/slowest process or cycle time -> llm_process_facts\n"
          "- WHEN is X building, next weeks, planned qty -> llm_demand_weekly\n"
          "- what did we ACTUALLY build, how recently -> llm_builds\n"
          "- what IS model X, family, description, BOM size -> llm_model_facts\n"
          "- BOM materials list -> model_bom tool; trend over weeks -> completion_trend tool\n"
          "For open questions the run_sql tool queries these tables:\n"
        + facts.ddl()
        + "\n\nDOMAIN REFERENCE — what our words mean and today's live facts. "
          "This describes the DATA, never the limits of what you may write:\n"
        + glossary.system_prompt()
    )


def _ask_rich(question, history, done, emit, delta):
    """Native tool-calling for a cloud-class model. The rails stay — resolve,
    the cage, receipts — but the model decides which tools, how many, and what
    the answer looks like. This exists because the one-tool pipeline muzzled a
    120B into 'I can only provide cycle-time completion' on a comparison
    question the tools could trivially feed."""
    from modules.cycle_time.chat import openai_compat, sqllane
    schemas = tools.schema() + [_RUN_SQL_TOOL]
    messages = [{"role": "system", "content": _rich_system()}]
    for h in (history or [])[-8:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        if h.get("content"):
            messages.append({"role": role, "content": str(h["content"])[:2000]})
    messages.append({"role": "user", "content": question})

    calls, sources, sqls, last_rows = [], [], [], None
    reminded = False
    for _ in range(_MAX_ROUNDS):
        try:
            # The CLOUD directly, never llm.chat: its per-call fallback would
            # hand these tool schemas to the local 8B, which cannot drive the
            # loop (and Ollama 400s on the schema format).
            msg = openai_compat.chat(messages, tools=schemas)
            llm.note_answered(llm.MODEL)
        except llm.LLMError as e:
            log.warning("chat agent loop lost the cloud (%s)", e)
            if not calls:
                return None                      # classic local pipeline instead
            break                                # compose from what was gathered
        tcs = msg.get("tool_calls") or []
        if not tcs:
            text = _strip_meta((msg.get("content") or "").strip())
            if not text:
                break
            emit("delta", text)
            lane = "cycletime" if calls else "general"
            out = done(text, lane, intent="agent",
                       grounded=any(c["ok"] for c in calls),
                       calls=calls, sources=sources)
            if sqls:
                out["sql"] = "\n".join(sqls)
            if last_rows and (last_rows["row_count"] > 1 or len(last_rows["columns"]) > 1):
                out["table"] = {"columns": last_rows["columns"],
                                "rows": last_rows["rows"]}
            return out

        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": tcs})
        for tc in tcs[:4]:
            name = (tc.get("function") or {}).get("name") or ""
            try:
                args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            except ValueError:
                args = {}
            emit("stage", f"{name}...")
            if name == "run_sql":
                result = sqllane.execute_checked(str(args.get("sql") or ""), question)
                if result.get("sql"):
                    sqls.append(result["sql"])
                if not result.get("error"):
                    last_rows = {k: result[k] for k in ("columns", "rows", "row_count")}
            else:
                result = tools.call(name, args)
            ok = "error" not in result
            calls.append({"tool": name, "args": args, "ok": ok})
            src = result.get("_src")
            if src and src not in sources:
                sources.append(src)
            log.info("chat agent: %s(%s) -> %s", name, str(args)[:120],
                     "ok" if ok else "error:" + str(result.get("error")))
            slim = {k: (f"[{len(v)} items, first 25: " + json.dumps(v[:25], default=str) + "]"
                        if isinstance(v, list) and len(v) > 25 else v)
                    for k, v in result.items()}
            messages.append({"role": "tool", "tool_call_id": tc.get("id") or name,
                             "content": json.dumps(slim, default=str)[:5000]})

        # Positioned LAST on purpose: the depth rule sits mid-way through a 5k
        # system prompt and gpt-oss ignored it — "how many models with no
        # cycle time in asp" came back as eight words. Models weigh the end of
        # the context; the reminder lands right where the answer gets written.
        if calls and not reminded:
            reminded = True
            messages.append({"role": "system", "content":
                "When you answer: be thorough. If models are the subject, LIST "
                "them (assembly + one key fact each, markdown bullets or a "
                "table). State the ALL-vs-Planned split when they differ. End "
                "with one line of insight or context. A bare count is not an "
                "answer."})

    # Rounds exhausted or empty reply - answer from EVERYTHING gathered, not
    # the last fetch: a comparison that lost the cloud after two workcell
    # pulls still has both workcells in hand.
    emit("stage", "writing...")
    gathered = [m for m in messages if m.get("role") == "tool"]
    if gathered:
        combined = {"results": [json.loads(g["content"]) for g in gathered[-6:]]}
        text = _compose(question, [combined], "", delta)
    else:
        text = "I could not work that one out - try naming a workcell or model."
    out = done(text, "cycletime", intent="agent",
               grounded=any(c["ok"] for c in calls), calls=calls, sources=sources)
    if sqls:
        out["sql"] = "\n".join(sqls)
    return out


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
    llm.reset_answered()          # threadpool threads are reused; no stale label
    emit = on_event or (lambda kind, text: None)
    delta = (lambda t: emit("delta", t)) if on_event else None

    def done(answer: str, lane: str, *, intent: str = "none", grounded: bool = False,
             calls: list | None = None, sources: list | None = None,
             error: str | None = None) -> dict:
        # The 1200-char guard was sized for one-sentence 8B answers; it would
        # truncate a thorough analyst answer mid-table.
        cap = 6000 if llm.rich() else _MAX_ANSWER_CHARS
        out = {"answer": answer[:cap].strip(), "lane": lane,
               "intent": intent, "grounded": grounded,
               "calls": calls or [], "sources": sources or [],
               "model": llm.answered_by() or llm.MODEL, "elapsed_s": round(time.time() - t0, 1)}
        if error:
            out["error"] = error
        _log_turn(question, out)
        return out

    # L0 — before the availability check on purpose: a greeting deserves a
    # greeting even while Ollama is down.
    canned = router.instant(question)
    if canned:
        return done(canned, "instant")

    ok, detail = llm.available()
    if not ok:
        log.warning("chat unavailable: %s", detail)
        return done("The model is not available right now - the local engine "
                    "is down and no cloud model is reachable. Try again in a "
                    "minute; if it persists, restart Ollama (or the machine).",
                    "error", error="ollama_unavailable")

    # A cloud-class brain gets the agent loop: its own tool choices, its own
    # rounds. The deterministic fast paths above (instant, exact definitions)
    # still run first because free beats smart.
    if llm.rich():
        exact = glossary.define(question) if router.concept_question(question) else None
        if exact:
            return done(exact, "cycletime", grounded=True, sources=["glossary"])
        out = _ask_rich(question, history, done, emit, delta)
        if out is not None:
            return out
        # Cloud died before gathering anything — the classic local pipeline
        # below answers instead. Slower and terser, never an error bubble.

    # L1 — one structured call. Degrades to a lexicon guess, never raises.
    emit("stage", "routing…")
    r = router.repair(router.route(question, history), question)

    if r["domain"] == "general":
        try:
            emit("stage", "writing…")
            return done(_general(question, history, _general_prompt(), delta), "general")
        except llm.LLMError as e:
            log.warning("chat model failed: %s", e)
            return done("The model hit an error mid-answer. Try again - if it "
                        "keeps happening, the local engine needs a restart.",
                        "error", error="ollama_failed")

    if r["intent"] == "none":
        # In-domain but no tool answers it — usually a concept question ("what
        # does cannot_check mean"). The glossary IS that answer's source, and
        # when exactly one known term is named the definition is served
        # verbatim: instant, exact, and immune to paraphrase.
        # Only when the question READS like a definition — intent=none can
        # also be the router's degraded fallback, and "how many % complete for
        # keysight" contains the word "complete", which define() would match.
        exact = glossary.define(question) if router.concept_question(question) else None
        if exact:
            return done(exact, "cycletime", grounded=True, sources=["glossary"])
        try:
            emit("stage", "writing…")
            return done(_general(question, history, glossary.system_prompt(), delta),
                        "cycletime", sources=["glossary"])
        except llm.LLMError as e:
            log.warning("chat model failed: %s", e)
            return done("The model hit an error mid-answer. Try again - if it "
                        "keeps happening, the local engine needs a restart.",
                        "error", error="ollama_failed")

    if r["intent"] == "open_query":
        from modules.cycle_time.chat import sqllane
        emit("stage", "writing a query…")
        result = sqllane.run(question)
        calls = [{"tool": "open_query", "args": {}, "ok": "error" not in result}]
        if result.get("error"):
            out = done("I could not build a safe query for that — try naming a "
                       "workcell, a status, or a measure from the data.",
                       "cycletime", intent="open_query", calls=calls)
        elif not result["rows"]:
            out = done("The query ran but matched nothing.", "cycletime",
                       intent="open_query", grounded=True, calls=calls,
                       sources=[result["_src"]])
        else:
            # The model writes the PROSE; the numbers themselves ship as
            # structured rows the UI renders as a real table. The prose cannot
            # replace the table — only accompany it — so a paraphrase slip is
            # visible against the rows printed under it. A rich model sees the
            # full result and answers every part of the question; the local 8B
            # sees 8 rows and writes one lead-in sentence.
            emit("stage", "writing…")
            n_rows = 40 if llm.rich() else 8
            slim = {"question_result": True, "columns": result["columns"],
                    "rows": result["rows"][:n_rows], "row_count": result["row_count"],
                    "_src": result["_src"]}
            text = _compose(question, [slim], "", delta)
            if text.startswith("Here is what I found"):   # compose gave up
                text = f"{result['row_count']} result rows below."
            out = done(text, "cycletime", intent="open_query", grounded=True,
                       calls=calls, sources=[result["_src"]])
            if result["row_count"] > 1 or len(result["columns"]) > 1:
                out["table"] = {"columns": result["columns"],
                                "rows": result["rows"]}
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
