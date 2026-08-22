"""
api/routers/universe_chat.py
────────────────────────────
The chat client's API — what the Ask page and the drawer call.

  POST   /api/universe/chat              what useChat sends → AI SDK UI message stream (SSE)
  GET    /api/universe/threads           my chats, newest first
  GET    /api/universe/threads/{id}      one chat with its messages (UIMessage parts)
  PATCH  /api/universe/threads/{id}      {title}
  DELETE /api/universe/threads/{id}
  POST   /api/universe/feedback          {message_id, vote: 1|-1|0, reason?}

Pilot gate: UNIVERSE_CHAT_USERS (comma list of NTIDs). Everyone else is 403.
A thread is visible only to its owner. Spec: docs/superpowers/specs/2026-08-23-universe-chat-design.md
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.auth import verified_ntid
from modules.universe import config as C
from modules.universe.chat import loop, stream, threads

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/universe", tags=["universe-chat"])

MODEL_FN = None       # the chain by default (lazy); tests swap in a scripted model


def _model_fn():
    global MODEL_FN
    if MODEL_FN is None:
        from modules.universe.eval import chain
        MODEL_FN = chain.chat
    return MODEL_FN


def _model_label() -> str:
    from modules.universe.eval import chain
    if MODEL_FN is chain.chat:
        trace = chain.take_trace()
        chain.take_events()
        return "chain: " + " -> ".join(dict.fromkeys(trace)) if trace else "chain"
    return getattr(MODEL_FN, "__name__", "model")


def pilot(ntid: str = Depends(verified_ntid)) -> str:
    if ntid.lower() not in C.CHAT_USERS:
        log.warning("chat denied %s — not in UNIVERSE_CHAT_USERS", ntid)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The chat is in pilot — ask Faiz for access.")
    return ntid.lower()


# ─── threads ─────────────────────────────────────────────────────────────────

class Rename(BaseModel):
    title: str


class Feedback(BaseModel):
    message_id: str
    vote: int
    reason: str | None = None


@router.get("/threads")
def list_threads(ntid: str = Depends(pilot)):
    return threads.list_for(ntid)


@router.get("/threads/{thread_id}")
def get_thread(thread_id: str, ntid: str = Depends(pilot)):
    t = threads.get(thread_id, ntid)
    if t is None:
        raise HTTPException(404, "No such chat.")
    return t


@router.patch("/threads/{thread_id}")
def rename_thread(thread_id: str, body: Rename, ntid: str = Depends(pilot)):
    if not threads.rename(thread_id, ntid, body.title):
        raise HTTPException(404, "No such chat.")
    return {"ok": True}


@router.delete("/threads/{thread_id}")
def delete_thread(thread_id: str, ntid: str = Depends(pilot)):
    if not threads.delete(thread_id, ntid):
        raise HTTPException(404, "No such chat.")
    return {"ok": True}


@router.post("/feedback")
def feedback(body: Feedback, ntid: str = Depends(pilot)):
    if body.vote not in (-1, 0, 1):
        raise HTTPException(400, "vote must be 1, -1 or 0")
    if not threads.feedback(body.message_id, ntid, body.vote, body.reason):
        raise HTTPException(404, "No such message.")
    return {"ok": True}


# ─── chat ────────────────────────────────────────────────────────────────────

def _text_of(message: dict) -> str:
    parts = message.get("parts") or []
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip() or str(message.get("content") or "").strip()


@router.post("/chat")
def chat(body: dict, ntid: str = Depends(pilot)):
    msgs = body.get("messages") or []
    users = [m for m in msgs if m.get("role") == "user"]
    if not users:
        raise HTTPException(400, "No question.")
    question = _text_of(users[-1])
    if not question:
        raise HTTPException(400, "No question.")
    if len(question) > 2000:
        raise HTTPException(400, "Question too long (max 2000 characters).")

    tid = body.get("thread_id") or body.get("id")
    thread = threads.get(tid, ntid) if tid else None
    if thread is None:
        thread = threads.create(ntid, question)
        thread["messages"] = []
    # history comes from the store, not the client: text parts only, in order
    history = [{"role": m["role"], "content": "\n".join(p.get("text", "") for p in m["parts"] if p.get("type") == "text")}
               for m in thread["messages"] if m["role"] in ("user", "assistant")]
    history = [h for h in history if h["content"]]
    history.append({"role": "user", "content": question})
    threads.add_message(thread["id"], "user", [{"type": "text", "text": question}])

    message_id = uuid.uuid4().hex
    model_fn = _model_fn()

    def gen():
        events: list[tuple] = []

        def tee():
            for ev in loop.run(history, model_fn):
                events.append(ev)
                yield ev
        try:
            yield from stream.sse(tee(), message_id=message_id)
        finally:
            parts = stream.parts(events)
            if parts:
                threads.add_message(thread["id"], "assistant", parts, model=_model_label())

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={**stream.HEADERS, "x-thread-id": thread["id"]})
