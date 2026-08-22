"""
tests/test_universe_chat.py
───────────────────────────
The chat client's backend: one loop (events), the AI SDK stream, saved threads,
feedback, the pilot gate. Plain python — `python tests/test_universe_chat.py`.
Written before the code (spec: docs/superpowers/specs/2026-08-23-universe-chat-design.md).
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ─── the loop: messages in, events out ──────────────────────────────────────

def _fake_model(script):
    """A model that answers from a script: each call pops the next reply."""
    replies = list(script)

    def call(messages, tools_spec, tool_choice="auto"):
        r = replies.pop(0)
        if isinstance(r, str):
            return {"content": r, "tool_calls": None, "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        name, args = r
        return {"content": "", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "tool_calls": [{"id": f"call_{len(replies)}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)}}]}
    return call


def test_loop_emits_tool_call_tool_result_text_and_done():
    """One question → the model describes a view, queries it, answers. The loop emits
    the four event kinds in order and the final text is the answer."""
    from modules.universe.chat import loop
    model = _fake_model([("universe_describe", {"view": "v_workcell"}),
                         ("universe_query", {"sql": "select count(*) as n from v_workcell"}),
                         "There are 111 workcell rows."])
    events = list(loop.run([{"role": "user", "content": "how many workcells"}], model))
    kinds = [e[0] for e in events]
    assert kinds == ["tool_call", "tool_result", "tool_call", "tool_result", "text", "done"], kinds
    call = events[0][1]
    assert call["name"] == "universe_describe" and call["args"] == {"view": "v_workcell"} and call["id"]
    result = events[3][1]
    assert result["id"] == events[2][1]["id"] and result["ok"] and result["rows"] == 1, result
    assert events[4][1] == "There are 111 workcell rows."
    done = events[5][1]
    assert done["stopped"] == "answered" and done["rounds"] == 3 and done["sqls"], done
    assert done["usage"]["prompt_tokens"] == 30


def test_loop_pushes_a_model_that_narrates_sql_to_run_it():
    """An answer that shows SQL it never ran is a plan, not an answer — one nudge, then it runs."""
    from modules.universe.chat import loop
    model = _fake_model(["I would run:\n```sql\nselect count(*) as n from v_workcell\n```",
                         ("universe_query", {"sql": "select count(*) as n from v_workcell"}),
                         "111 rows."])
    events = list(loop.run([{"role": "user", "content": "how many"}], model))
    assert [e[0] for e in events] == ["tool_call", "tool_result", "text", "done"], [e[0] for e in events]
    assert events[2][1] == "111 rows."


def test_exam_answer_record_is_built_from_the_same_loop():
    """The exam harness consumes loop.run — one loop, two consumers. Its record keeps the
    shape the grader reads: tool_calls with result_text, sqls, answer, stopped, rounds."""
    from modules.universe.eval import run as R
    model = _fake_model([("universe_describe", {"view": "v_workcell"}), "Done: 111."])
    rec = R.answer({"id": 1, "text": "list all workcells"}, model)
    assert rec["stopped"] == "answered" and rec["answer"] == "Done: 111." and rec["rounds"] == 2
    assert rec["tool_calls"][0]["name"] == "universe_describe" and "v_workcell" in rec["tool_calls"][0]["result_text"]
    assert rec["usage"]["prompt_tokens"] == 20


# ─── the stream: events → AI SDK UI message stream v1 ───────────────────────

def test_stream_writes_ai_sdk_v1_lines():
    from modules.universe.chat import stream
    events = [("tool_call", {"id": "c1", "name": "universe_query", "args": {"sql": "select 1"}}),
              ("tool_result", {"id": "c1", "name": "universe_query", "ok": True, "rows": 1, "output_text": '{"rows":[{"1":1}]}'}),
              ("text", "One row."),
              ("done", {"stopped": "answered", "rounds": 2, "sqls": ["select 1"], "usage": {}, "model": "chain"})]
    lines = list(stream.sse(events, message_id="m1"))
    assert all(l.startswith("data: ") and l.endswith("\n\n") for l in lines), lines[:2]
    chunks = [json.loads(l[6:]) for l in lines[:-1]]
    assert chunks[0] == {"type": "start", "messageId": "m1"}
    assert chunks[1] == {"type": "tool-input-available", "toolCallId": "c1", "toolName": "universe_query", "input": {"sql": "select 1"}}
    assert chunks[2]["type"] == "tool-output-available" and chunks[2]["toolCallId"] == "c1" and chunks[2]["output"]["rows"] == 1
    assert [c["type"] for c in chunks[3:6]] == ["text-start", "text-delta", "text-end"] and chunks[4]["delta"] == "One row."
    assert chunks[3]["id"] == chunks[4]["id"] == chunks[5]["id"]
    assert chunks[-1] == {"type": "finish"}
    assert lines[-1] == "data: [DONE]\n\n"
    assert stream.HEADERS["x-vercel-ai-ui-message-stream"] == "v1"


def test_stream_turns_a_loop_error_into_an_error_chunk_then_finish():
    from modules.universe.chat import stream
    lines = list(stream.sse([("error", "every slot is down")], message_id="m2"))
    chunks = [json.loads(l[6:]) for l in lines[:-1]]
    assert chunks[1] == {"type": "error", "errorText": "every slot is down"}
    assert chunks[-1]["type"] == "finish" and lines[-1] == "data: [DONE]\n\n"


def test_stream_parts_mirror_what_was_streamed():
    """The parts saved for a reopened chat are the UIMessage parts useChat would have built."""
    from modules.universe.chat import stream
    events = [("tool_call", {"id": "c1", "name": "universe_describe", "args": {"view": "v_workcell"}}),
              ("tool_result", {"id": "c1", "name": "universe_describe", "ok": True, "rows": None, "output_text": "v_workcell ..."}),
              ("text", "111 rows."), ("done", {"stopped": "answered", "rounds": 2, "sqls": [], "usage": {}, "model": "chain"})]
    parts = stream.parts(events)
    assert parts[0] == {"type": "tool-universe_describe", "toolCallId": "c1", "state": "output-available",
                        "input": {"view": "v_workcell"}, "output": {"ok": True, "rows": None, "text": "v_workcell ..."}}
    assert parts[1] == {"type": "text", "text": "111 rows."}


# ─── threads: saved per user, reopened exactly ──────────────────────────────

def _temp_db():
    from core import database
    d = tempfile.mkdtemp()
    database.DB_PATH = Path(d) / "test.db"
    database.init_db()


def test_threads_round_trip_per_user():
    from modules.universe.chat import threads
    _temp_db()
    t = threads.create("faiz", "how many workcells are in P1")
    assert t["id"] and t["title"] == "how many workcells are in P1"
    threads.add_message(t["id"], "user", [{"type": "text", "text": "how many workcells are in P1"}])
    m = threads.add_message(t["id"], "assistant", [{"type": "text", "text": "18."}], model="chain: gemini")
    mine = threads.list_for("faiz")
    assert [x["id"] for x in mine] == [t["id"]] and mine[0]["title"] == t["title"]
    assert threads.list_for("someone_else") == []
    full = threads.get(t["id"], "faiz")
    assert [x["role"] for x in full["messages"]] == ["user", "assistant"]
    assert full["messages"][1]["parts"] == [{"type": "text", "text": "18."}] and full["messages"][1]["id"] == m["id"]
    assert threads.get(t["id"], "someone_else") is None
    threads.rename(t["id"], "faiz", "P1 workcells")
    assert threads.get(t["id"], "faiz")["title"] == "P1 workcells"
    threads.delete(t["id"], "faiz")
    assert threads.list_for("faiz") == []


def test_thread_title_is_the_first_question_trimmed():
    from modules.universe.chat import threads
    _temp_db()
    t = threads.create("faiz", "x" * 200)
    assert len(t["title"]) == 60


def test_feedback_is_stored_on_the_message():
    from modules.universe.chat import threads
    _temp_db()
    t = threads.create("faiz", "q")
    m = threads.add_message(t["id"], "assistant", [{"type": "text", "text": "a"}])
    assert threads.feedback(m["id"], "faiz", -1, "wrong workcell") is True
    msg = threads.get(t["id"], "faiz")["messages"][0]
    assert msg["feedback"] == -1 and msg["feedback_reason"] == "wrong workcell"
    assert threads.feedback(m["id"], "someone_else", 1, None) is False     # not their message


# ─── the router: pilot gate, owner-only threads ─────────────────────────────

def _client(ntid: str):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.auth import verified_ntid
    from api.routers.universe_chat import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verified_ntid] = lambda: ntid
    return TestClient(app)


def test_router_gate_allows_pilot_and_rejects_others():
    import os
    from modules.universe import config as C
    _temp_db()
    os.environ["UNIVERSE_CHAT_USERS"] = "faiz, aisyah"
    C.reload_chat_users()
    assert _client("FAIZ").get("/api/universe/threads").status_code == 200      # case-insensitive
    assert _client("stranger").get("/api/universe/threads").status_code == 403


def test_router_threads_are_owner_only():
    import os
    from modules.universe import config as C
    from modules.universe.chat import threads
    _temp_db()
    os.environ["UNIVERSE_CHAT_USERS"] = "faiz, aisyah"
    C.reload_chat_users()
    t = threads.create("faiz", "q")
    assert _client("faiz").get(f"/api/universe/threads/{t['id']}").status_code == 200
    assert _client("aisyah").get(f"/api/universe/threads/{t['id']}").status_code == 404
    assert _client("aisyah").delete(f"/api/universe/threads/{t['id']}").status_code == 404
    assert _client("faiz").patch(f"/api/universe/threads/{t['id']}", json={"title": "renamed"}).status_code == 200
    assert _client("faiz").get(f"/api/universe/threads/{t['id']}").json()["title"] == "renamed"


def test_router_chat_streams_and_saves_both_messages():
    """POST /chat with what useChat sends: the stream comes back in v1 shape and the
    thread holds the question and the answer (with its tool parts) afterwards."""
    import os
    from modules.universe import config as C
    from modules.universe.chat import threads
    from api.routers import universe_chat
    _temp_db()
    os.environ["UNIVERSE_CHAT_USERS"] = "faiz"
    C.reload_chat_users()
    universe_chat.MODEL_FN = _fake_model([("universe_describe", {"view": "v_workcell"}), "111 rows."])
    t = threads.create("faiz", "how many")
    body = {"id": t["id"], "thread_id": t["id"], "trigger": "submit-message",
            "messages": [{"id": "u1", "role": "user", "parts": [{"type": "text", "text": "how many workcells"}]}]}
    r = _client("faiz").post("/api/universe/chat", json=body)
    assert r.status_code == 200 and r.headers["x-vercel-ai-ui-message-stream"] == "v1", r.text[:200]
    types = [json.loads(l[6:])["type"] for l in r.text.split("\n\n") if l.startswith("data: {")]
    assert types[0] == "start" and "tool-input-available" in types and "text-delta" in types and types[-1] == "finish"
    saved = threads.get(t["id"], "faiz")["messages"]
    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert saved[1]["parts"][0]["type"] == "tool-universe_describe" and {"type": "text", "text": "111 rows."} in saved[1]["parts"]
    assert saved[1]["parts"][-1]["type"] == "data-model"        # who answered, last
    assert saved[1]["model"]
    # the saved answer keeps the id the stream announced, so the page's thumbs find it
    start = next(json.loads(l[6:]) for l in r.text.split("\n\n") if l.startswith('data: {"type":"start"'))
    assert saved[1]["id"] == start["messageId"], (saved[1]["id"], start)


def test_router_chat_without_a_thread_creates_one():
    import os
    from modules.universe import config as C
    from modules.universe.chat import threads
    from api.routers import universe_chat
    _temp_db()
    os.environ["UNIVERSE_CHAT_USERS"] = "faiz"
    C.reload_chat_users()
    universe_chat.MODEL_FN = _fake_model(["Hello."])
    body = {"id": "new", "trigger": "submit-message",
            "messages": [{"id": "u1", "role": "user", "parts": [{"type": "text", "text": "hello there"}]}]}
    r = _client("faiz").post("/api/universe/chat", json=body)
    assert r.status_code == 200
    assert r.headers["x-thread-id"], "the new thread id comes back in a header so the page can adopt it"
    mine = threads.list_for("faiz")
    assert len(mine) == 1 and mine[0]["title"] == "hello there"


# ─── which model answered, and the models panel ─────────────────────────────

def test_stream_carries_the_model_label_as_a_data_part():
    """The page shows "answered by …" beside the thumbs: the loop's consumer appends a
    ("model", label) event; it streams as a data-model chunk and is stored as a part."""
    from modules.universe.chat import stream
    events = [("text", "Hi."), ("model", "chain: gemini-3.7-flash -> groq-gpt-oss-120b"),
              ("done", {"stopped": "answered", "rounds": 1, "sqls": [], "usage": {}})]
    chunks = [json.loads(l[6:]) for l in list(stream.sse(events, message_id="m"))[:-1]]
    assert {"type": "data-model", "data": {"label": "chain: gemini-3.7-flash -> groq-gpt-oss-120b"}} in chunks
    assert chunks.index(next(c for c in chunks if c["type"] == "data-model")) < chunks.index({"type": "finish"})
    parts = stream.parts(events)
    assert parts[-1] == {"type": "data-model", "data": {"label": "chain: gemini-3.7-flash -> groq-gpt-oss-120b"}}


def test_router_chat_streams_the_model_label_and_stores_it():
    import os
    from modules.universe import config as C
    from modules.universe.chat import threads
    from api.routers import universe_chat
    _temp_db()
    os.environ["UNIVERSE_CHAT_USERS"] = "faiz"
    C.reload_chat_users()
    universe_chat.MODEL_FN = _fake_model(["Hello."])
    body = {"id": "new", "trigger": "submit-message",
            "messages": [{"id": "u1", "role": "user", "parts": [{"type": "text", "text": "hello"}]}]}
    r = _client("faiz").post("/api/universe/chat", json=body)
    chunks = [json.loads(l[6:]) for l in r.text.split("\n\n") if l.startswith("data: {")]
    assert any(c["type"] == "data-model" and c["data"]["label"] for c in chunks), [c["type"] for c in chunks]
    saved = threads.list_for("faiz")[0]
    msg = threads.get(saved["id"], "faiz")["messages"][1]
    assert msg["parts"][-1]["type"] == "data-model" and msg["parts"][-1]["data"]["label"] == msg["model"]


def test_chain_status_reports_usage_against_known_limits_and_resets_daily():
    """Each slot: calls and tokens today, the free tier's daily limits where known,
    a usage percent, and when the count resets. A new UTC day zeroes the counters."""
    from modules.universe.eval import chain
    s = chain.Slot("groq-gpt-oss-120b", "http://x", None, "openai/gpt-oss-120b", rpd=1000, tpd=200_000)
    s.calls, s.tokens = 10, 50_000
    old = chain.SLOTS
    chain.SLOTS = [s]
    try:
        row = chain.status()[0]
        assert row["limits"] == {"rpd": 1000, "tpd": 200_000}
        assert row["usage_pct"] == 25                          # tokens 50k/200k beats calls 10/1000
        assert row["resets_at"].endswith("+00:00") and "T00:00:00" in row["resets_at"]
        chain._day = "2000-01-01"                             # pretend the counters are from yesterday
        row = chain.status()[0]
        assert row["calls"] == 0 and row["tokens"] == 0 and row["usage_pct"] == 0
    finally:
        chain.SLOTS = old
    assert all(isinstance(x.rpd, (int, type(None))) for x in chain.SLOTS)


def test_router_models_panel_lists_every_slot():
    import os
    from modules.universe import config as C
    _temp_db()
    os.environ["UNIVERSE_CHAT_USERS"] = "faiz"
    C.reload_chat_users()
    r = _client("faiz").get("/api/universe/chat/models")
    assert r.status_code == 200
    rows = r.json()["models"]
    assert len(rows) >= 10 and {"slot", "model", "key", "calls", "tokens", "limits", "usage_pct", "cooldown_s", "resets_at"} <= set(rows[0])
    assert r.json()["note"]
    assert _client("stranger").get("/api/universe/chat/models").status_code == 403


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:                       # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {str(e)[:300]}")
            traceback.print_exc(limit=2)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
