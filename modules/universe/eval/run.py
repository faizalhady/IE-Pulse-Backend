"""
modules/universe/eval/run.py
────────────────────────────
The exam harness. A model answers each pool question through the three tools;
every call, every SQL and the final answer are recorded; the checks grade what
can be graded mechanically. Faiz grades the rest.

    python -m modules.universe.eval.run                 # Groq, every question
    python -m modules.universe.eval.run --only 1 5      # a subset
    python -m modules.universe.eval.run --provider ollama

Provider config comes from the backend .env: CHAT_API_BASE (default Groq),
CHAT_API_KEY, CHAT_MODEL. OLLAMA_BASE_URL / OLLAMA_MODEL for the local control.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

from modules.universe import tools as T
from modules.universe.eval import questions as Q

ROOT = Path(__file__).resolve().parents[3]
RUNS = Path(__file__).resolve().parent / "runs"

# Groq's free tier caps gpt-oss-120b at ~8k tokens per request, max_tokens
# included. Everything below is sized to stay under it: small tool results, a
# character budget on the conversation, old results trimmed to a stub.
CONTEXT_BUDGET_CHARS = 16_000     # ~4k tokens of messages
TOOL_RESULT_CHARS = 3_500
KEEP_FULL_RESULTS = 2             # the newest N tool results stay verbatim
MAX_TOKENS = 1_500

TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "universe_describe",
        "description": "The universe's views and their columns, each with its meaning in Jabil's words. Call this FIRST before writing SQL. Pass a view name for one view, nothing for all.",
        "parameters": {"type": "object", "properties": {"view": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "universe_query",
        "description": "Run ONE read-only DuckDB SELECT over the views (v_workcell, v_units_out_daily, v_output_daily, v_ole_weekly, v_ole_daily, v_process, v_cycle_time, v_route, v_demand, v_fpy_daily). Capped at 200 rows — aggregate, filter, ORDER BY with LIMIT. Only views are reachable.",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "universe_define",
        "description": "What a Jabil word means and the traps around it (workcell, OLE, terminal step, fiscal year, AOP, bay, SMH, FPY …). Use before a knowledge answer or when a column comment is not enough.",
        "parameters": {"type": "object", "properties": {"term": {"type": "string"}}, "required": ["term"]}}},
]

SYSTEM = """You are the analyst for Jabil Penang's Industrial Engineering team, answering from the Jabil Universe — one data model of the plant.

Rules that are not optional:
- Workcell = CUSTOMER (KEYSIGHT, WABTEC …). Never a station or a line.
- Every number you state must come from a tool result. Never estimate a figure you did not fetch.
- Views: v_workcell, v_units_out_daily, v_output_daily, v_ole_weekly, v_ole_daily, v_process, v_cycle_time, v_route, v_demand, v_fpy_daily. Call universe_describe with ONE view name before querying it; the column comments carry meaning the names do not. Results are capped at 40 rows — aggregate and filter; never list raw rows you do not need.
- "How many workcells" has several true answers (active / inactive, customer / support) — say which.
- "Which plant" is two facts: physical and governing. Say which you used.
- Units are boards counted once at the model's terminal step — not scan rows.
- Two cycle times exist: the study (standard, work content) and the MES scan delta (elapsed). Never mix them.
- The scans cover 9 Jul → 8 Aug 2026 only; the OLE share history reaches back to March and counts differently (v_output_daily.source). Say which you used.
- Bay identities are not reconciled; equipment capacity is an authored seed; defect codes do not exist. When a question needs one of these, say so plainly instead of guessing.
- Never name a column you have not seen in a universe_describe result. If a query fails, describe the view, then retry — do not guess.
- Routes are per line: step_order restarts for each line_id. Pick one line (or group by it) before listing steps end to end.
- For a knowledge question, call universe_define for EACH term before answering, and quote the formula as defined.
- When part of a question cannot be answered (bays, capacity, defect codes), say so in one line and answer the rest WITH numbers — demand, output, cycle time are always available.
- Show the SQL you used in the answer (a short code block per query).
- Be concise. Tables for numbers. One paragraph of reasoning for an analysis, with what you could not know."""


# ─── the loop ────────────────────────────────────────────────────────────────

def _dispatch(name: str, args: dict) -> dict:
    if name == "universe_describe":
        return {"result": T.describe_compact(args.get("view") or None)}
    if name == "universe_query":
        return {"result": T.query(args.get("sql") or "", max_rows=T.MODEL_ROWS)}
    if name == "universe_define":
        return {"result": T.define(args.get("term") or "")}
    return {"result": {"error": f"unknown tool {name}"}}


def answer(question: dict, model_fn, max_rounds: int = 8) -> dict:
    """Run one question. model_fn(messages, tools_spec) -> {"content": str} or
    {"tool_calls": [...]} in OpenAI shape (plus optional "usage")."""
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question["text"]}]
    rec = {"id": question["id"], "question": question["text"], "tool_calls": [], "sqls": [],
           "rounds": 0, "stopped": "", "answer": "", "usage": {"prompt_tokens": 0, "completion_tokens": 0},
           "started": datetime.now().isoformat(timespec="seconds")}
    t0 = time.time()
    for rnd in range(max_rounds):
        rec["rounds"] += 1
        _trim(messages)
        last = rnd == max_rounds - 1
        if last:
            messages.append({"role": "user", "content": "Your tool budget is used up. Write the final answer now from the results above. Do not call any tool."})
        try:
            # the last round keeps the tool schema (some models emit a call anyway and
            # the API rejects a call with no schema) but forbids choosing one
            try:
                msg = model_fn(messages, TOOLS_SPEC, "none" if last else "auto")
            except TypeError:
                msg = model_fn(messages, TOOLS_SPEC)
        except Exception as e:                     # noqa: BLE001
            rec["stopped"] = f"error: {str(e)[:300]}"
            break
        u = msg.get("usage") or {}
        rec["usage"]["prompt_tokens"] += int(u.get("prompt_tokens", 0))
        rec["usage"]["completion_tokens"] += int(u.get("completion_tokens", 0))
        tcs = msg.get("tool_calls") or []
        if not tcs:
            rec["answer"] = (msg.get("content") or "").strip()
            rec["stopped"] = "answered"
            break
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
        for tc in tcs:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            out = _dispatch(name, args)["result"]
            text = out if isinstance(out, str) else json.dumps(out, default=str, separators=(",", ":"))
            ok = not (isinstance(out, dict) and out.get("error"))
            rec["tool_calls"].append({"name": name, "args": args, "ok": ok, "result_text": text[:20000],
                                      "rows": out.get("row_count") if isinstance(out, dict) else None})
            if name == "universe_query" and isinstance(out, dict) and out.get("sql"):
                rec["sqls"].append(out["sql"])
            if len(text) > TOOL_RESULT_CHARS:
                text = text[:TOOL_RESULT_CHARS] + f" …[truncated; {len(text)} chars — aggregate or filter for less]"
            messages.append({"role": "tool", "tool_call_id": tc.get("id") or name, "content": text})
    else:
        rec["stopped"] = "round cap"
    rec["elapsed_s"] = round(time.time() - t0, 1)
    return rec


def _trim(messages: list[dict]) -> None:
    """Keep the conversation under the budget: older tool results become a stub
    (the model already used them), newest KEEP_FULL_RESULTS stay verbatim."""
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idx[:-KEEP_FULL_RESULTS] if len(tool_idx) > KEEP_FULL_RESULTS else []:
        c = messages[i]["content"]
        if len(c) > 300:
            messages[i]["content"] = c[:240] + " …[older result trimmed]"
    total = sum(len(m.get("content") or "") for m in messages)
    i = 0
    while total > CONTEXT_BUDGET_CHARS and i < len(tool_idx):
        c = messages[tool_idx[i]]["content"]
        if len(c) > 120:
            messages[tool_idx[i]]["content"] = c[:100] + " …[trimmed]"
            total = sum(len(m.get("content") or "") for m in messages)
        i += 1


def grade(rec: dict) -> dict:
    q = next(q for q in Q.QUESTIONS if q["id"] == rec["id"])
    checks = []
    for name, fn in Q.GENERIC_CHECKS + q["checks"]:
        try:
            ok = bool(fn(rec))
        except Exception as e:                     # noqa: BLE001
            ok = False
            rec.setdefault("notes", []).append(f"check {name!r} raised {e}")
        checks.append({"name": name, "passed": ok})
    return {"checks": checks, "passed": sum(c["passed"] for c in checks), "total": len(checks)}


# ─── providers (OpenAI-compatible chat completions) ─────────────────────────

def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:                              # noqa: BLE001
        pass


def openai_compatible(base: str, key: str, model: str, temperature: float = 0.0):
    def call(messages, tools_spec, tool_choice="auto"):
        body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": MAX_TOKENS}
        if tools_spec:
            body.update({"tools": tools_spec, "tool_choice": tool_choice})
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"       # the budget is tokens per minute, not brains
        for attempt in range(4):
            r = httpx.post(f"{base.rstrip('/')}/chat/completions", json=body, timeout=180,
                           headers={"Authorization": f"Bearer {key}"} if key else {})
            if r.status_code == 429 and attempt < 3:
                m = re.search(r"try again in ([\d.]+)s", r.text)
                time.sleep(min(float(m.group(1)) if m else 5.0, 30.0) + 0.5)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            msg = data["choices"][0]["message"]
            return {"content": msg.get("content"), "tool_calls": msg.get("tool_calls"), "usage": data.get("usage", {})}
        raise RuntimeError("rate limited")
    return call


def provider(name: str):
    override = os.environ.get("CHAT_MODEL")
    _load_env()
    if override:
        os.environ["CHAT_MODEL"] = override
    if name == "groq":
        base = os.getenv("CHAT_API_BASE", "https://api.groq.com/openai/v1")
        return openai_compatible(base, os.getenv("CHAT_API_KEY", ""), os.getenv("CHAT_MODEL", "openai/gpt-oss-120b")), os.getenv("CHAT_MODEL", "openai/gpt-oss-120b")
    if name == "ollama":
        base = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434") + "/v1"
        model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
        return openai_compatible(base, "", model), model
    raise SystemExit(f"unknown provider {name}")


# ─── the run ─────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="groq")
    ap.add_argument("--only", nargs="*", type=int)
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--regrade", help="re-grade an existing run directory with the current checks and rewrite its REPORT.md")
    ap.add_argument("--model", help="override the provider's model (e.g. openai/gpt-oss-20b when the 120b daily cap is spent)")
    a = ap.parse_args()
    if a.regrade:
        d = Path(a.regrade)
        recs = []
        for f in sorted(d.glob("q*.json"), key=lambda p: int(p.stem[1:])):
            rec = json.loads(f.read_text(encoding="utf-8"))
            rec.pop("notes", None)
            rec["grade"] = grade(rec)
            f.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
            recs.append(rec)
        _report(d, recs, recs[0].get("model", "?") if recs else "?")
        print(f"{d.name}: {sum(r['grade']['passed'] for r in recs)}/{sum(r['grade']['total'] for r in recs)}")
        return
    if a.model:
        os.environ["CHAT_MODEL"] = a.model
        os.environ["OLLAMA_MODEL"] = a.model
    model_fn, model_name = provider(a.provider)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{a.provider}-{model_name.split('/')[-1]}"
    out = RUNS / stamp
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for q in Q.QUESTIONS:
        if a.only and q["id"] not in a.only:
            continue
        print(f"Q{q['id']} …", end=" ", flush=True)
        rec = answer(q, model_fn, max_rounds=a.rounds)
        rec["model"] = model_name
        rec["grade"] = grade(rec)
        (out / f"q{q['id']}.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
        print(f"{rec['stopped']} · {rec['rounds']} rounds · {rec['elapsed_s']}s · {rec['grade']['passed']}/{rec['grade']['total']}")
        summary.append(rec)
    _report(out, summary, model_name)
    print(f"\n{out}")


def _report(out: Path, recs: list[dict], model: str) -> None:
    lines = [f"# Universe LLM trial — {model}", "", f"Run: `{out.name}` · {len(recs)} questions · "
             f"{sum(r['grade']['passed'] for r in recs)}/{sum(r['grade']['total'] for r in recs)} checks passed", "",
             "| Q | question | stopped | rounds | s | prompt tok | grade | failed checks |", "|---|---|---|---|---|---|---|---|"]
    for r in recs:
        failed = [c["name"] for c in r["grade"]["checks"] if not c["passed"]]
        lines.append(f"| {r['id']} | {r['question'][:60]} | {r['stopped']} | {r['rounds']} | {r['elapsed_s']} | "
                     f"{r['usage']['prompt_tokens']} | {r['grade']['passed']}/{r['grade']['total']} | {'; '.join(failed)} |")
    for r in recs:
        lines += ["", f"## Q{r['id']} — {r['question']}", "", f"**Stopped:** {r['stopped']} · rounds {r['rounds']} · {r['elapsed_s']} s", "",
                  "**Tools:** " + ", ".join(f"{c['name']}({'ok' if c['ok'] else 'ERR'}{', ' + str(c['rows']) + ' rows' if c.get('rows') is not None else ''})" for c in r["tool_calls"]), ""]
        for s in r["sqls"]:
            lines += ["```sql", s, "```"]
        lines += ["", "**Answer:**", "", r["answer"] or "_(none)_", ""]
        if r.get("notes"):
            lines += ["**Notes:** " + " · ".join(r["notes"]), ""]
        lines += ["**Checks:** " + " · ".join(f"{'✅' if c['passed'] else '❌'} {c['name']}" for c in r["grade"]["checks"]), ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
