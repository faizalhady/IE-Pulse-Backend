"""
modules/universe/chat/loop.py
─────────────────────────────
ONE loop: a conversation in, events out. The chat page streams the events; the
exam harness (eval/run.py) folds them into a graded record. Lifted from the
harness's answer() on 2026-08-23 so the two can never drift.

    for kind, payload in run(messages, model_fn):
        kind: "tool_call"   {id, name, args}
              "tool_result" {id, name, ok, rows, output_text}
              "text"        str            (one per answer — the chain is not token-streamed)
              "done"        {stopped, rounds, sqls, usage}
              "error"       str
"""

from __future__ import annotations

import json
import re
from typing import Callable, Iterator

from modules.universe import tools as T

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
        "description": "Run ONE read-only DuckDB SELECT over the views (v_workcell, v_units_out_daily, v_output_daily, v_ole_weekly, v_ole_daily, v_process, v_cycle_time, v_route, v_demand, v_fpy_daily, v_employee, v_headcount, v_paid_hours_weekly, v_department, v_scan_point, v_bay, v_bay_occupancy, v_bay_activity, v_line, v_asset, v_equipment). Capped at 200 rows — aggregate, filter, ORDER BY with LIMIT. Only views are reachable.",
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
- Views: v_workcell, v_units_out_daily, v_output_daily, v_ole_weekly, v_ole_daily, v_process, v_cycle_time, v_route, v_demand, v_fpy_daily, v_employee, v_headcount, v_paid_hours_weekly, v_department, v_scan_point, v_bay, v_bay_occupancy, v_bay_activity, v_line, v_asset, v_equipment. Call universe_describe with ONE view name before querying it; the column comments carry meaning the names do not. Results are capped at 40 rows — aggregate and filter; never list raw rows you do not need.
- "How many workcells" has several true answers (active / inactive, customer / support) — say which.
- "Which plant" is two facts: physical and governing. Say which you used.
- Units are boards counted once at the model's terminal step — not scan rows.
- Two cycle times exist: the study (standard, work content) and the MES scan delta (elapsed). Never mix them.
- The scans cover 9 Jul → 22 Aug 2026; the OLE share history reaches back to March and counts differently (v_output_daily.source). Say which you used.
- Bays: v_bay_activity says where a workcell's boards were scanned, by week, in MES bay names; v_bay_occupancy holds the declared / configured occupancy with its evidence. The layout names (BAY 15A) and the MES names (BAY 105) are two schemes, not reconciled - say which you used. Equipment capacity is an authored seed; defect codes do not exist. When a question needs one of these, say so plainly instead of guessing.
- People: v_employee has names, departments and workcells; v_headcount and v_paid_hours_weekly are the counts. Machines: v_equipment is what the scans saw (a floor, not the fleet); v_asset is the EST1C + SAP register (lifecycle says installed or scrapped).
- Never name a column you have not seen in a universe_describe result. If a query fails, describe the view, then retry — do not guess.
- Routes are per line: step_order restarts for each line_id. Pick one line (or group by it) before listing steps end to end.
- For a knowledge question, call universe_define for EACH term before answering, and quote the formula as defined.
- When part of a question cannot be answered (bays, capacity, defect codes), say so in one line and answer the rest WITH numbers — demand, output, cycle time are always available.
- A capacity or what-if question needs three things before any verdict: the demand (v_demand), the standard time per unit (v_cycle_time) and recent output (v_units_out_daily). Read all three; say what is still missing (bay ids, machine counts) only after that.

How to write the answer:
- Lead with the answer. The first line or two is what was asked, with the number.
- No fixed shape: let the question choose. A figure → the figure and a small table. A comparison → a table and two lines of reading. "Draft an email" or "explain" → paragraphs. Never a template.
- Readable and short. No padding, no restating the question, no narrating what you did ("I queried…") — the tool steps are shown separately.
- Do not paste SQL into the answer; it is shown with the tool step. Mention a query only if asked how.
- Say the window or as_of of the data once, briefly. Say in one line what could not be known, then stop.
"""


def _dispatch(name: str, args: dict) -> dict:
    if name == "universe_describe":
        return {"result": T.describe_compact(args.get("view") or None)}
    if name == "universe_query":
        return {"result": T.query(args.get("sql") or "", max_rows=T.MODEL_ROWS)}
    if name == "universe_define":
        return {"result": T.define(args.get("term") or "")}
    return {"result": {"error": f"unknown tool {name}"}}


def _norm_sql(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().rstrip(";")).lower()


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


def run(history: list[dict], model_fn: Callable, max_rounds: int = 8) -> Iterator[tuple]:
    """history: [{role: user|assistant, content}] — the question is the last one.
    model_fn(messages, tools_spec, tool_choice) -> {content, tool_calls, usage}."""
    messages = [{"role": "system", "content": SYSTEM}, *[{"role": m["role"], "content": m["content"]} for m in history]]
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    sqls: list[str] = []
    rounds, nudged = 0, False
    for rnd in range(max_rounds):
        rounds += 1
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
            yield ("error", str(e)[:300])
            yield ("done", {"stopped": f"error: {str(e)[:300]}", "rounds": rounds, "sqls": sqls, "usage": usage})
            return
        u = msg.get("usage") or {}
        usage["prompt_tokens"] += int(u.get("prompt_tokens", 0))
        usage["completion_tokens"] += int(u.get("completion_tokens", 0))
        tcs = msg.get("tool_calls") or []
        if not tcs:
            content = (msg.get("content") or "").strip()
            # an answer that shows SQL it never ran is a plan, not an answer (weaker models
            # narrate the query instead of calling the tool) — once, push it to run the query
            ran = " ".join(_norm_sql(q) for q in sqls)
            unrun = [q for q in re.findall(r"```sql\s*(.*?)```", content, re.S | re.I) if _norm_sql(q) not in ran]
            if unrun and not last and not nudged:
                nudged = True
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": "You wrote SQL but did not run it. Call universe_query with that SQL now, then answer from its result."})
                continue
            yield ("text", content)
            yield ("done", {"stopped": "answered", "rounds": rounds, "sqls": sqls, "usage": usage})
            return
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
        for tc in tcs:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            call_id = tc.get("id") or name
            yield ("tool_call", {"id": call_id, "name": name, "args": args})
            out = _dispatch(name, args)["result"]
            text = out if isinstance(out, str) else json.dumps(out, default=str, separators=(",", ":"))
            ok = not (isinstance(out, dict) and out.get("error"))
            rows = out.get("row_count") if isinstance(out, dict) else None
            if name == "universe_query" and isinstance(out, dict) and out.get("sql"):
                sqls.append(out["sql"])
            yield ("tool_result", {"id": call_id, "name": name, "ok": ok, "rows": rows, "output_text": text[:20000]})
            if len(text) > TOOL_RESULT_CHARS:
                text = text[:TOOL_RESULT_CHARS] + f" …[truncated; {len(text)} chars — aggregate or filter for less]"
            messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
    yield ("done", {"stopped": "round cap", "rounds": rounds, "sqls": sqls, "usage": usage})
