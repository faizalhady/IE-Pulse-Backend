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

from modules.universe.chat import loop as L
from modules.universe.chat.loop import (CONTEXT_BUDGET_CHARS, KEEP_FULL_RESULTS, MAX_TOKENS,   # noqa: F401 — re-exported
                                        SYSTEM, TOOL_RESULT_CHARS, TOOLS_SPEC)


# ─── the loop lives in chat/loop.py; this folds its events into a graded record ──

def answer(question: dict, model_fn, max_rounds: int = 8) -> dict:
    """Run one question through chat/loop.run and keep the record shape the grader
    and the report read: tool_calls (with result_text), sqls, answer, stopped, rounds."""
    rec = {"id": question["id"], "question": question["text"], "tool_calls": [], "sqls": [],
           "rounds": 0, "stopped": "", "answer": "", "usage": {"prompt_tokens": 0, "completion_tokens": 0},
           "started": datetime.now().isoformat(timespec="seconds")}
    t0 = time.time()
    pending: dict[str, dict] = {}
    for kind, payload in L.run([{"role": "user", "content": question["text"]}], model_fn, max_rounds=max_rounds):
        if kind == "tool_call":
            pending[payload["id"]] = {"name": payload["name"], "args": payload["args"]}
        elif kind == "tool_result":
            call = pending.pop(payload["id"], {"name": payload["name"], "args": {}})
            rec["tool_calls"].append({"name": call["name"], "args": call["args"], "ok": payload["ok"],
                                      "result_text": payload["output_text"], "rows": payload["rows"]})
        elif kind == "text":
            rec["answer"] = payload
        elif kind == "done":
            rec["stopped"], rec["rounds"], rec["sqls"], rec["usage"] = payload["stopped"], payload["rounds"], payload["sqls"], payload["usage"]
    rec["elapsed_s"] = round(time.time() - t0, 1)
    return rec


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
    if name == "chain":
        from modules.universe.eval import chain
        return chain.chat, "chain"
    if name == "ollama":
        base = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434") + "/v1"
        model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
        return openai_compatible(base, "", model), model
    raise SystemExit(f"unknown provider {name}")


# ─── the run ─────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="groq", help="groq | chain (the free-model fallback loop, eval/chain.py) | ollama")
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
        if a.provider == "chain":
            from modules.universe.eval import chain
            rec["chain_trace"] = chain.take_trace()          # which slot served each round
            rec["chain_events"] = chain.take_events()        # why slots were skipped (429 -> 60s, HTTP 402 …)
            rec["model"] = "chain: " + " -> ".join(dict.fromkeys(rec["chain_trace"])) if rec["chain_trace"] else "chain"   # ascii: cp1252 console
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
